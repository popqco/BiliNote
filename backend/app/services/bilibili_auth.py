"""B 站扫码登录与 Cookie 自动续期。

为什么需要这个模块（2026-10-11）：B 站 Cookie（SESSDATA）会被服务端主动失效，
手动粘贴的裸 SESSDATA 没有 refresh_token，只能反复手动更新。扫码登录成功时
B 站会额外返回 refresh_token（网页端 localStorage 里的 ac_time_value），配合
官方 Cookie 刷新协议可以无限续期——只要在到期前跑一次刷新，登录态就一直是活的。

API 与协议参考 bilibili-API-collect 文档（原仓库 2026-01 被下架，引用备份仓库
hbhyw/bilibili-API-collect-backup）：
- 扫码登录：docs/login/login_action/QR.md
- Cookie 刷新：docs/login/cookie_refresh.md（RSA 公钥、correspondPath、confirm 流程）
"""
import re
import time
import binascii
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

import requests
from Cryptodome.Cipher import PKCS1_OAEP
from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import RSA

from app.services.cookie_manager import CookieConfigManager, extract_cookie_value
from app.utils.bilibili_http import bilibili_session
from app.utils.logger import get_logger

logger = get_logger(__name__)

PLATFORM = "bilibili"

PASSPORT_BASE = "https://passport.bilibili.com"
GENERATE_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate"
POLL_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll"
COOKIE_INFO_URL = f"{PASSPORT_BASE}/x/passport-login/web/cookie/info"
COOKIE_REFRESH_URL = f"{PASSPORT_BASE}/x/passport-login/web/cookie/refresh"
COOKIE_CONFIRM_URL = f"{PASSPORT_BASE}/x/passport-login/web/confirm/refresh"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
CORRESPOND_URL = "https://www.bilibili.com/correspond/1/{path}"

# 扫码轮询状态码（docs/login/login_action/QR.md）
_QR_STATUS = {0: "confirmed", 86101: "waiting", 86090: "scanned", 86038: "expired"}

# B 站官方 web 端 wasm 内置的 RSA 公钥（bilibili-API-collect 文档记录，用于
# correspondPath 加密；若 B 站换公钥，刷新会报 404/格式错误，更新此常量即可）
_REFRESH_RSA_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

# refresh_csrf 藏在 correspond 页面的 <div id="1-name">xxx</div> 里
_REFRESH_CSRF_RE = re.compile(r'id="1-name">([^<]+)<')

_TIMEOUT = 15
# 「是否需要刷新」探测的节流间隔：cookie/info 是廉价 GET，但没必要每轮自动化都打
_CHECK_THROTTLE_SECONDS = 12 * 3600


class BilibiliAuthError(RuntimeError):
    """带操作指引的 B 站登录态异常。"""


def _session() -> requests.Session:
    # B 站官方接口一律直连：bilibili_session 已关 trust_env（环境/系统代理免疫）
    s = bilibili_session()
    s.headers.update({"Referer": "https://www.bilibili.com/"})
    return s


def _json(resp: requests.Response, action: str) -> Dict[str, Any]:
    try:
        data = resp.json()
    except Exception as e:
        raise BilibiliAuthError(f"{action}失败：响应不是 JSON（{e}）")
    if data.get("code") not in (0,):
        raise BilibiliAuthError(
            f"{action}失败：code={data.get('code')}, msg={data.get('message')}"
        )
    return data


def _cookie_dict_from_response(resp: requests.Response) -> Dict[str, str]:
    """从响应的 Set-Cookie 里收集本次下发的 cookie（domain 匹配由 requests 完成）。"""
    return {k: v for k, v in resp.cookies.get_dict().items() if v}


def build_cookie_string(old_cookie: str, new_cookies: Dict[str, str]) -> str:
    """新下发的 cookie 覆盖旧串里的同名键，保留 buvid3 等设备指纹类键不动。"""
    merged: Dict[str, str] = {}
    for part in str(old_cookie or "").split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            if k and v:
                merged[k.strip()] = v.strip()
    merged.update(new_cookies)
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def get_username(cookie: str) -> Optional[str]:
    """用当前 cookie 查登录用户名（nav 接口），失败返回 None。"""
    try:
        resp = _session().get(NAV_URL, headers={"Cookie": cookie}, timeout=_TIMEOUT)
        data = resp.json().get("data") or {}
        return data.get("uname") if data.get("isLogin") else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------

def start_qr_login() -> Dict[str, Any]:
    """申请一个登录二维码，返回 {qr_url, qrcode_key, expires_in}（180 秒有效）。"""
    data = _json(_session().get(GENERATE_URL, timeout=_TIMEOUT), "获取登录二维码").get("data") or {}
    qr_url, key = data.get("url"), data.get("qrcode_key")
    if not qr_url or not key:
        raise BilibiliAuthError("获取登录二维码失败：响应缺少 url/qrcode_key")
    return {"qr_url": qr_url, "qrcode_key": key, "expires_in": 180}


def poll_qr_login(qrcode_key: str) -> Dict[str, Any]:
    """轮询扫码结果。

    返回 {status: waiting|scanned|confirmed|expired}；confirmed 时额外带
    {cookie, refresh_token, username}，且凭据已持久化到 downloader.json。
    """
    resp = _session().get(POLL_URL, params={"qrcode_key": qrcode_key}, timeout=_TIMEOUT)
    payload = _json(resp, "轮询扫码状态")
    data = payload.get("data") or {}
    status = _QR_STATUS.get(data.get("code"), "waiting")
    if status != "confirmed":
        return {"status": status}

    refresh_token = data.get("refresh_token")
    new_cookies = _cookie_dict_from_response(resp)
    if not new_cookies.get("SESSDATA") and data.get("url"):
        # 兜底：个别环境 Set-Cookie 被剥掉时，跨域登录 url 的查询参数里有全套 cookie
        qs = parse_qs(urlparse(data["url"]).query)
        for k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
            if k in qs and not new_cookies.get(k):
                new_cookies[k] = qs[k][0]
    if not new_cookies.get("SESSDATA") or not new_cookies.get("bili_jct"):
        raise BilibiliAuthError("扫码成功但响应里拿不到 SESSDATA/bili_jct，请重试")

    mgr = CookieConfigManager()
    old_cookie = mgr.get(PLATFORM) or ""
    cookie = build_cookie_string(old_cookie, new_cookies)
    mgr.set_auth(PLATFORM, cookie, refresh_token=refresh_token, refreshed_at=time.time())
    logger.info("B 站扫码登录成功，已写入完整 Cookie 与 refresh_token")
    return {
        "status": "confirmed",
        "cookie": cookie,
        "refresh_token": refresh_token,
        "username": get_username(cookie),
    }


# ---------------------------------------------------------------------------
# Cookie 自动续期
# ---------------------------------------------------------------------------

def _correspond_path(ts_ms: int) -> str:
    """correspondPath = RSA-OAEP(SHA-256) 加密 `refresh_{毫秒时间戳}` 后的小写 hex。"""
    cipher = PKCS1_OAEP.new(RSA.import_key(_REFRESH_RSA_PEM), hashAlgo=SHA256)
    return binascii.b2a_hex(cipher.encrypt(f"refresh_{ts_ms}".encode())).decode()


def _do_refresh(mgr: CookieConfigManager, auth: Dict[str, Any]) -> Dict[str, Any]:
    cookie = auth["cookie"]
    refresh_token = auth.get("refresh_token") or extract_cookie_value(cookie, "ac_time_value")
    csrf = extract_cookie_value(cookie, "bili_jct")
    if not csrf:
        raise BilibiliAuthError("Cookie 里没有 bili_jct，无法刷新（请重新扫码登录）")
    if not refresh_token:
        raise BilibiliAuthError(
            "缺少 refresh_token（手动粘贴的裸 SESSDATA 无法续期）：请到「设置 → 下载配置」扫码登录"
        )

    sess = _session()
    old_headers = {"Cookie": cookie}

    ts_ms = int(time.time() * 1000)
    resp = sess.get(CORRESPOND_URL.format(path=_correspond_path(ts_ms)), headers=old_headers, timeout=_TIMEOUT)
    m = _REFRESH_CSRF_RE.search(resp.text or "")
    if resp.status_code >= 400 or not m:
        raise BilibiliAuthError(
            f"获取 refresh_csrf 失败（correspond 页 {resp.status_code}）：correspondPath 可能已过期或公钥已变更"
        )
    refresh_csrf = m.group(1).strip()

    resp = sess.post(
        COOKIE_REFRESH_URL,
        data={"csrf": csrf, "refresh_csrf": refresh_csrf, "source": "main_web", "refresh_token": refresh_token},
        headers=old_headers,
        timeout=_TIMEOUT,
    )
    payload = _json(resp, "刷新 Cookie")
    new_cookies = _cookie_dict_from_response(resp)
    if not new_cookies.get("SESSDATA") or not new_cookies.get("bili_jct"):
        raise BilibiliAuthError("刷新成功但响应里拿不到新 SESSDATA/bili_jct")
    new_refresh_token = (payload.get("data") or {}).get("refresh_token") or refresh_token
    new_cookie = build_cookie_string(cookie, new_cookies)

    # 确认刷新：让旧 refresh_token 对应的旧 Cookie 彻底作废（不做这步 B 站可能回滚）
    new_csrf = extract_cookie_value(new_cookie, "bili_jct")
    resp = sess.post(
        COOKIE_CONFIRM_URL,
        data={"csrf": new_csrf, "refresh_token": refresh_token},
        headers={"Cookie": new_cookie},
        timeout=_TIMEOUT,
    )
    try:
        confirm_code = resp.json().get("code")
    except Exception as e:
        raise BilibiliAuthError(f"确认刷新失败：响应不是 JSON（{e}）")
    if confirm_code != 0:
        raise BilibiliAuthError(f"确认刷新失败：code={confirm_code}（新 Cookie 已生效但旧凭据未作废）")

    mgr.set_auth(PLATFORM, new_cookie, refresh_token=new_refresh_token, refreshed_at=time.time())
    logger.info("B 站 Cookie 已自动续期，refresh_token 已轮换")
    return {"ok": True, "refreshed": True, "username": get_username(new_cookie)}


def ensure_cookie_fresh(force: bool = False) -> Dict[str, Any]:
    """检查并按需续期 B 站 Cookie；无凭据/未到期时为 no-op，绝不抛异常以外的影响。

    返回形如 {ok, refreshed?, skipped?, reason?, action?}：
    - ok=True  ：本轮检查完成（refreshed=True 表示真的续了期）
    - ok=False ：需要人工介入；action='qr_login' 表示该扫码了
    """
    mgr = CookieConfigManager()
    auth = mgr.get_auth(PLATFORM)
    if not auth.get("cookie"):
        return {"ok": False, "reason": "未配置 B 站 Cookie", "action": "qr_login"}

    last_check = float(auth.get("checked_at") or 0)
    if not force and time.time() - last_check < _CHECK_THROTTLE_SECONDS:
        return {"ok": True, "skipped": "recently_checked"}

    sess = _session()
    try:
        resp = sess.get(
            COOKIE_INFO_URL,
            params={"csrf": extract_cookie_value(auth["cookie"], "bili_jct") or ""},
            headers={"Cookie": auth["cookie"]},
            timeout=_TIMEOUT,
        )
        payload = resp.json()
    except Exception as e:
        mgr.mark_checked(PLATFORM)
        logger.warning(f"B 站 Cookie 状态探测失败（不阻塞本轮）：{e}")
        return {"ok": True, "skipped": f"probe_failed: {e}"}

    code = payload.get("code")
    if code == -101:
        mgr.mark_checked(PLATFORM)
        logger.warning("B 站 Cookie 已失效（cookie/info -101）")
        return {"ok": False, "reason": "Cookie 未登录或已失效", "action": "qr_login"}
    if code != 0:
        mgr.mark_checked(PLATFORM)
        logger.warning(f"B 站 cookie/info 返回异常 code={code}（不阻塞本轮）")
        return {"ok": True, "skipped": f"code={code}"}

    need_refresh = bool((payload.get("data") or {}).get("refresh"))
    if not need_refresh:
        mgr.mark_checked(PLATFORM)
        return {"ok": True, "refreshed": False}

    if not (auth.get("refresh_token") or extract_cookie_value(auth["cookie"], "ac_time_value")):
        mgr.mark_checked(PLATFORM)
        logger.warning("B 站提示需要刷新 Cookie，但缺少 refresh_token，无法自动续期")
        return {
            "ok": False,
            "reason": "缺少 refresh_token（手动粘贴的裸 SESSDATA 无法续期），请扫码登录",
            "action": "qr_login",
        }

    logger.info("B 站提示 Cookie 需要刷新，开始自动续期")
    try:
        result = _do_refresh(mgr, auth)
        result["ok"] = True
        return result
    except BilibiliAuthError as e:
        # 续期失败不致命：旧 Cookie 多半还能撑一阵，交给 watchlater 的 -101 告警兜底
        mgr.mark_checked(PLATFORM)
        logger.error(f"B 站 Cookie 自动续期失败：{e}")
        return {"ok": False, "reason": str(e), "action": "retry_later"}

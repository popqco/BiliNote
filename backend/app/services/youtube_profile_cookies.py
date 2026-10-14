"""从「专用浏览器配置」直接读取 YouTube 登录 Cookie（服务持久化免维护方案）。

原理：Chromium/Edge 只对**默认** user-data-dir 启用 App-Bound 加密（v20，需要
Elevation Service 且进程校验无法绕过）。换用独立的非默认配置目录时，浏览器退回
经典 DPAPI 方案（v10），而 DPAPI 是同用户可解的——于是应用可以在**不启动浏览器**
的前提下，直接从配置目录的 Cookies 数据库解出明文 Cookie。

用户只需在该专用配置里登录一次 YouTube（`yt-cookie-export/youtube_login_export.py`），
此后每次下载年龄限制视频时由本模块现取最新 Cookie；浏览器运行时还会自动轮换
__Secure-1PSIDTS 等短周期令牌，配置目录常开常新。

安全边界：只读本机当前用户的浏览器数据，解出的 Cookie 只写临时文件交给 yt-dlp，
不落库、不离开本机。
"""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from typing import List, Optional, Tuple

from app.services.cookie_manager import CookieConfigManager, iter_cookie_pairs
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 专用配置目录（与登录脚本约定一致；可用环境变量覆盖）
DEFAULT_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".bilinote-youtube-profile")
PROFILE_ENV = "BILINOTE_YOUTUBE_PROFILE"
KEEP_DOMAINS = ("youtube.com", "google.com")


def get_profile_dir() -> str:
    return os.environ.get(PROFILE_ENV) or DEFAULT_PROFILE_DIR


def _dpapi_unprotect(blob: bytes) -> bytes:
    """调用 Windows DPAPI 解密（当前用户上下文）。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _profile_key(profile_dir: str) -> Optional[bytes]:
    """从 Local State 取出 DPAPI 保护的 v10 主密钥（32 字节 AES key）。"""
    ls_path = os.path.join(profile_dir, "Local State")
    if not os.path.isfile(ls_path):
        return None
    with open(ls_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    enc = (state.get("os_crypt") or {}).get("encrypted_key")
    if not enc:
        return None
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None
    return _dpapi_unprotect(raw[5:])


def _decrypt_cookie(key: bytes, host_key: str, encrypted_value: bytes) -> Optional[str]:
    """解 v10 cookie（AES-256-GCM）；meta>=24 的库还有 32 字节域哈希前缀。"""
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        return None
    if not encrypted_value or encrypted_value[:3] != b"v10":
        # v20（App-Bound）或其它形态：本方案不处理
        return None
    nonce = encrypted_value[3:15]
    body = encrypted_value[15:]
    if len(body) < 17:
        return None
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plain = cipher.decrypt_and_verify(body[:-16], body[-16:])
    except Exception:
        return None
    # Chromium 24+ 在明文前附加 32 字节 SHA256(host_key)，用它来判定并剥掉
    if len(plain) > 32 and hashlib.sha256(host_key.encode("utf-8")).digest() == plain[:32]:
        plain = plain[32:]
    return plain.decode("utf-8", "replace")


def profile_cookies(profile_dir: Optional[str] = None) -> List[dict]:
    """读取专用配置，返回 [{domain,name,value,secure,path,expires}]（已解密明文）。

    **必须保留每个 cookie 自己的域名**：HSID/SSID 这类签名 cookie 在 .google.com 与
    .youtube.com 下的值不同，混用/去重会导致 YouTube 侧会话签名不匹配（表现为
    "Sign in to confirm your age"，明明带了 cookie 却算未登录）。

    任何一步失败都返回空列表——调用方会退回手工粘贴的 Cookie 或匿名下载。
    """
    profile_dir = profile_dir or get_profile_dir()
    db_path = os.path.join(profile_dir, "Default", "Network", "Cookies")
    if not os.path.isfile(db_path):
        return []
    if not os.path.isfile(os.path.join(profile_dir, "Local State")):
        return []
    try:
        key = _profile_key(profile_dir)
    except Exception as e:
        logger.warning(f"读取专用配置密钥失败: {e}")
        return []
    if not key:
        return []

    # 浏览器可能正在使用该库，复制一份再读，避免锁/半写状态
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        shutil.copy2(db_path, tmp.name)
        con = sqlite3.connect(tmp.name)
        try:
            rows = con.execute(
                "select host_key, name, encrypted_value, value, is_secure, path, expires_utc "
                "from cookies"
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.warning(f"读取专用配置 Cookie 库失败: {e}")
        return []
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    out: List[dict] = []
    seen = set()
    for host_key, name, encrypted_value, plain_value, is_secure, path, expires_utc in rows:
        if not any(d in (host_key or "") for d in KEEP_DOMAINS):
            continue
        if (host_key, name) in seen:
            continue
        value = plain_value or None
        if value is None and encrypted_value:
            value = _decrypt_cookie(key, host_key, encrypted_value)
        if value is None or value == "":
            continue
        seen.add((host_key, name))
        # Chromium 的 expires_utc 是 1601 年起的微秒；转换失败就用一年后兜底
        expires = 0
        try:
            if expires_utc:
                expires = int(float(expires_utc) / 1_000_000 - 11644473600)
        except (TypeError, ValueError):
            expires = 0
        out.append({
            "domain": host_key or ".youtube.com",
            "name": name,
            "value": value,
            "secure": bool(is_secure),
            "path": path or "/",
            "expires": expires,
        })
    return out


def profile_cookie_pairs(profile_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """(name, value) 视图，供 requests/字幕等只需键值的场景使用。"""
    return [(c["name"], c["value"]) for c in profile_cookies(profile_dir)]


def profile_cookie_text(profile_dir: Optional[str] = None) -> Optional[str]:
    """返回 `k=v; k=v` 形式的 Cookie 串（无可用登录 Cookie 时 None）。

    只有拿到 SID/LOGIN_INFO 这类登录标志才算可用，避免把未登录的半套 Cookie
    当成有效凭据（半套反而可能让 yt-dlp 判断为可疑会话）。
    """
    pairs = profile_cookie_pairs(profile_dir)
    if not pairs:
        return None
    names = {n for n, _ in pairs}
    if not {"SID", "LOGIN_INFO"} & names:
        return None
    return "; ".join(f"{n}={v}" for n, v in pairs)


def resolve_youtube_cookies():
    """返回 (cookies, source)：cookies 为 [{domain,name,value,...}] 列表。

    优先「专用浏览器配置」（免维护、浏览器自动轮换），其次用户在设置里手工粘贴的
    Cookie（此时统一按 .youtube.com 域处理）；都没有则 ([], 'none')。
    供下载器 / 元信息 / 字幕三处共用，避免各自实现导致行为不一致。
    """
    cookies = []
    try:
        cookies = profile_cookies()
    except Exception as e:
        logger.warning(f"读取专用浏览器配置 Cookie 失败，回退手工配置: {e}")
        cookies = []
    if cookies:
        names = {c["name"] for c in cookies}
        if {"SID", "LOGIN_INFO"} & names:
            return cookies, "profile"
    raw = CookieConfigManager().get("youtube")
    pairs = list(iter_cookie_pairs(raw))
    by_name = {}
    for name, value in pairs:
        by_name.setdefault(name, value)
    return [
        {"domain": ".youtube.com", "name": n, "value": v, "secure": True,
         "path": "/", "expires": 0}
        for n, v in by_name.items()
    ], ("config" if raw else "none")


def resolve_youtube_cookie_pairs():
    """(name, value) 视图（兼容旧调用）。"""
    cookies, source = resolve_youtube_cookies()
    return [(c["name"], c["value"]) for c in cookies], source

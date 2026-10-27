"""微信生态（公众号文章 / 视频号）的解析、下载与解密工具。

两条链路的边界（2026-10-24 实测）：

* 公众号文章：文章 HTML 里的 ``mp_video_trans_info`` 能拿到带签名直链，
  但腾讯 CDN 对纯 HTTP 请求固定 403（签名绑定真实浏览器会话）。这里用
  Playwright 驱动系统 Edge 打开文章、点开视频，再用同一 context 的
  ``context.request`` 分片下载。已验证 206 + video/mp4。
* 视频号：匿名预览接口只返回标题/作者/封面，不返回播放地址。开源方案
  （ltaoo/wx_channels_download、x554960766/wechat-mp-tools）都依赖腾讯
  元宝的登录态：先把分享链接交给元宝换成 token/eid，再调视频号预览接口
  拿 videoUrl + decodeKey。元宝 Cookie 由用户在设置页扫码登录后保存。

许可证说明：视频号解析流程参考了上述 MIT 项目公开的接口事实；ISAAC64
算法为公开算法（Hanson/WechatSphDecrypt），此处为独立 Python 实现。
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import requests

from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir
from app.utils.proc import no_window_kwargs

logger = get_logger(__name__)

ARTICLE_HOST = "mp.weixin.qq.com"
CHANNELS_HOSTS = ("weixin.qq.com", "channels.weixin.qq.com")
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
YUA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL / id
# ---------------------------------------------------------------------------

def is_wechat_article_url(url: str) -> bool:
    try:
        p = urlparse(str(url or "").strip())
    except Exception:
        return False
    return p.hostname == ARTICLE_HOST and p.path.startswith("/s")


def is_wechat_channels_url(url: str) -> bool:
    try:
        p = urlparse(str(url or "").strip())
    except Exception:
        return False
    if p.hostname not in CHANNELS_HOSTS:
        return False
    return p.path.startswith("/sph/") or p.path.startswith("/mobile/sf/")


def normalize_wechat_url(url: str) -> str:
    """公众号短链里偶尔把下划线写成 ``~``（微信读书导出常见）。"""
    raw = str(url or "").strip()
    if not raw:
        return raw
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.hostname == ARTICLE_HOST and p.path.startswith("/s/"):
        sid = p.path[3:].strip("/")
        if "~" in sid:
            return raw.replace(f"/s/{sid}", f"/s/{sid.replace('~', '_')}", 1)
    return raw


def extract_wechat_article_id(url: str) -> Optional[str]:
    raw = normalize_wechat_url(url)
    p = urlparse(raw)
    if p.hostname != ARTICLE_HOST:
        return None
    m = re.search(r"/s/([A-Za-z0-9_-]{10,})", p.path)
    if m:
        return m.group(1)
    q = parse_qs(p.query)
    sn = (q.get("sn") or [""])[0]
    if sn:
        return sn
    return None


def extract_channels_short_id(url: str) -> Optional[str]:
    raw = str(url or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.hostname not in CHANNELS_HOSTS:
        return None
    m = re.search(r"/(?:sph|mobile/sf)/([A-Za-z0-9_-]+)", p.path)
    return m.group(1) if m else None


def extract_wechat_video_id(url: str, platform: str) -> Optional[str]:
    if platform == "wechat_channels":
        return extract_channels_short_id(url)
    return extract_wechat_article_id(url)


# ---------------------------------------------------------------------------
# 公众号文章
# ---------------------------------------------------------------------------

def _decode_js_url(raw: str) -> str:
    """把文章脚本里的转义直链还原成可请求的 URL。"""
    s = str(raw or "")
    s = s.replace("\\x26amp;", "&amp;").replace("\\x26", "&")
    s = s.replace("\\x3d", "=").replace("\\x3f", "?").replace("\\x2f", "/")
    s = html.unescape(s)
    return s.strip().strip("'\"")


def _first(patterns: List[str], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.S)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _clean_title(value: str) -> str:
    title = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return title[:180]


def _parse_article_video_urls(text: str) -> List[Dict]:
    """抽取文章里的 mpvideo 直链与清晰度信息。"""
    found: List[Dict] = []
    # 典型结构：url: 'http://mpvideo...f10002.mp4?...'，同块内有 format_id/filesize。
    for m in re.finditer(r"url:\s*['\"]([^'\"]*mpvideo\.qpic\.cn[^'\"]*)['\"]", text):
        raw = m.group(1)
        block = text[max(0, m.start() - 900): m.end() + 400]
        fmt = re.search(r"format_id:\s*['\"]?(\d+)", block)
        size = re.search(r"filesize:\s*['\"]?(\d+)", block)
        dur = re.search(r"duration_ms:\s*['\"]?(\d+)", block)
        found.append({
            "url": _decode_js_url(raw),
            "format_id": int(fmt.group(1)) if fmt else 0,
            "filesize": int(size.group(1)) if size else 0,
            "duration_ms": int(dur.group(1)) if dur else 0,
        })
    # 老文章 / 单视频模板：cdn_url 直接放在 cgiData 里
    if not found:
        for m in re.finditer(r"cdn_url\s*:\s*['\"]([^'\"]*mpvideo\.qpic\.cn[^'\"]*)['\"]", text):
            found.append({"url": _decode_js_url(m.group(1)), "format_id": 0, "filesize": 0, "duration_ms": 0})
    # 去重 + 高码率优先（10002/10004 是公众号常见的“超清/流畅”档）
    seen = set()
    unique = []
    for item in found:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    unique.sort(key=lambda x: (x["format_id"], x["filesize"]), reverse=True)
    return unique


def parse_article_html(body: str) -> Dict:
    """从公众号文章 HTML 里抽标题/作者/封面/视频直链。"""
    title = _first([
        r"msg_title\s*=\s*['\"]([^'\"]+)['\"]",
        r"msg_title\s*:\s*['\"]([^'\"]+)['\"]",
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)',
        r"<title>(.*?)</title>",
    ], body)
    cover = _first([
        r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)',
        r"cdn_url\s*:\s*['\"]([^'\"]+)['\"]",
        r"cover_url\s*:\s*['\"]([^'\"]+)['\"]",
    ], body)
    author = _first([
        r"nick_name\s*:\s*['\"]([^'\"]+)['\"]",
        r"nickname\s*:\s*['\"]([^'\"]+)['\"]",
        r"author\s*:\s*['\"]([^'\"]+)['\"]",
    ], body)
    if not author or author.startswith("data-"):
        author = ""
    vid = _first([r"(wxv_[A-Za-z0-9_-]{10,})"], body)
    videos = _parse_article_video_urls(body)
    duration = max((v["duration_ms"] for v in videos), default=0) / 1000.0
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]
    return {
        "title": _clean_title(title),
        "cover_url": cover or None,
        "uploader": author or "微信公众号",
        "duration": duration,
        "video_id": vid or None,
        "videos": videos,
        "platform": "wechat_article",
    }


def fetch_article_html(url: str, timeout: int = 25) -> Dict:
    """匿名拉取公众号文章 HTML（公开文章无需登录）。"""
    article_url = normalize_wechat_url(url)
    session = requests.Session()
    session.trust_env = False  # 腾讯域名直连，别被系统代理拖慢/打断
    try:
        resp = session.get(
            article_url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        body = resp.text
        final_url = resp.url
    finally:
        session.close()
    meta = parse_article_html(body)
    meta["article_url"] = final_url
    if not meta.get("video_id"):
        meta["video_id"] = extract_wechat_article_id(final_url)
    if not meta.get("title"):
        meta["title"] = "微信公众号文章"
    return meta


def fetch_wechat_article_meta(url: str) -> Optional[Dict]:
    try:
        meta = fetch_article_html(url)
        return {k: meta.get(k) for k in ("title", "cover_url", "duration", "video_id", "platform")}
    except Exception as e:
        logger.warning(f"公众号文章元信息解析失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 视频号公开元信息 / 元宝解析
# ---------------------------------------------------------------------------

def _channels_headers(short_id: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_id}",
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
    }


def fetch_channels_public_meta(url: str) -> Dict:
    """匿名预览接口：标题/作者/封面/描述，不含播放地址。"""
    sid = extract_channels_short_id(url)
    if not sid:
        raise ValueError("不是有效的视频号分享链接")
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(
            "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info",
            json={"baseReq": {"generalToken": ""}, "shortUri": sid},
            headers=_channels_headers(sid),
            timeout=15,
        )
        data = resp.json()
    finally:
        session.close()
    if data.get("errCode") not in (0, None):
        raise RuntimeError(f"视频号接口返回错误：{data.get('errMsg') or data.get('errCode')}")
    feed = (data.get("data") or {}).get("feedInfo") or {}
    author = (data.get("data") or {}).get("authorInfo") or {}
    desc = feed.get("description") or feed.get("desc") or ""
    title = _clean_title(desc) or "视频号视频"
    return {
        "title": title,
        "description": desc,
        "cover_url": feed.get("coverUrl") or None,
        "uploader": author.get("nickname") or "视频号",
        "duration": 0,
        "video_id": sid,
        "platform": "wechat_channels",
        "webpage_url": f"https://weixin.qq.com/sph/{sid}",
        "raw_info": {"description": desc, "author": author.get("nickname") or ""},
    }


def _yuanbao_request(share_url: str, cookie: str) -> Dict:
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://yuanbao.tencent.com",
        "referer": "https://yuanbao.tencent.com/chat",
        "user-agent": YUA,
        "cookie": cookie,
        "x-source": "web",
    }
    payload = {"type": "video_channel_url", "url": share_url, "scene": 1}
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(
            "https://yuanbao.tencent.com/api/weixin/get_parse_result",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if resp.status_code == 401:
            raise RuntimeError("元宝登录态已失效，请重新扫码登录")
        if resp.status_code != 200:
            raise RuntimeError(f"元宝接口返回异常 (HTTP {resp.status_code})")
        return resp.json()
    finally:
        session.close()


def _channels_feed_info(export_id: str, token: str) -> Dict:
    rid = f"{int(time.time()):x}-{''.join(random.choices('0123456789abcdef', k=8))}"
    page = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    referer = (
        f"{page}?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={quote(token, safe='')}&entry_scene=0&eid={quote(export_id, safe='')}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": BROWSER_UA,
    }
    url = (
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
        f"?_rid={rid}&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
    )
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(
            url,
            json={"baseReq": {"generalToken": token}, "exportId": export_id},
            headers=headers,
            timeout=20,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"视频号预览接口返回异常 (HTTP {resp.status_code})")
        return resp.json()
    finally:
        session.close()


def resolve_channels_with_yuanbao(url: str, cookie: str) -> Dict:
    """用元宝登录态换取视频号播放地址；同时返回标题/封面/作者。"""
    sid = extract_channels_short_id(url)
    if not sid:
        raise ValueError("不是有效的视频号分享链接")
    share_url = f"https://weixin.qq.com/sph/{sid}"
    if not str(cookie or "").strip():
        raise RuntimeError("未配置腾讯元宝 Cookie")

    parsed = _yuanbao_request(share_url, cookie)
    if parsed.get("code") not in (0, None):
        raise RuntimeError(parsed.get("msg") or "元宝解析视频号失败，登录态可能已失效")
    data = parsed.get("data") or {}
    playable_url = data.get("playable_url") or ""
    if not playable_url:
        raise RuntimeError("元宝未返回可播放地址，登录态可能已失效")
    q = parse_qs(urlparse(playable_url).query)
    token = (q.get("token") or [""])[0]
    export_id = (q.get("eid") or [""])[0]
    if not token or not export_id:
        raise RuntimeError("元宝返回的地址缺少 token/eid")

    feed_resp = _channels_feed_info(export_id, token)
    if feed_resp.get("errCode") not in (0, None):
        raise RuntimeError(feed_resp.get("errMsg") or "视频号预览接口返回错误")
    data_wrap = feed_resp.get("data") or {}
    feed = data_wrap.get("feedInfo") or {}
    author = data_wrap.get("authorInfo") or {}

    video_url = feed.get("videoUrl") or ""
    h264 = (feed.get("h264VideoInfo") or {}).get("videoUrl") or ""
    h265 = (feed.get("h265VideoInfo") or {}).get("videoUrl") or ""
    if not video_url:
        video_url = h264 or h265
    decode_key = feed.get("decodeKey") or feed.get("decode_key") or ""
    media = feed.get("media") or []
    if (not video_url or not decode_key) and isinstance(media, list) and media:
        first = media[0] or {}
        video_url = video_url or first.get("url") or ""
        decode_key = decode_key or first.get("decodeKey") or ""
    if not decode_key and video_url:
        dq = parse_qs(urlparse(video_url).query)
        decode_key = (dq.get("decode_key") or dq.get("decodeKey") or [""])[0]

    desc = feed.get("description") or feed.get("desc") or ""
    title = _clean_title(desc) or _clean_title(feed.get("title") or "") or "视频号视频"
    duration = 0
    try:
        if feed.get("videoPlayLen"):
            duration = float(feed["videoPlayLen"])
        elif media and isinstance(media, list) and media[0].get("videoPlayLen"):
            duration = float(media[0]["videoPlayLen"])
    except Exception:
        duration = 0

    return {
        "title": title,
        "description": desc,
        "cover_url": feed.get("coverUrl") or (media[0].get("coverUrl") if media else None),
        "uploader": author.get("nickname") or "视频号",
        "duration": duration,
        "video_id": sid,
        "video_url": video_url,
        "decode_key": str(decode_key or ""),
        "platform": "wechat_channels",
        "webpage_url": share_url,
        "raw_info": {
            "description": desc,
            "author": author.get("nickname") or "",
            "export_id": export_id,
        },
    }


def fetch_wechat_channels_meta(url: str) -> Optional[Dict]:
    try:
        meta = fetch_channels_public_meta(url)
        return {k: meta.get(k) for k in ("title", "cover_url", "duration", "video_id", "platform")}
    except Exception as e:
        logger.warning(f"视频号元信息解析失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 下载 / 解密 / 转码
# ---------------------------------------------------------------------------

class Isaac64:
    """ISAAC64 PRNG（微信视频号只加密文件前 128 KiB 的密钥流发生器）。"""

    MASK = 0xFFFFFFFFFFFFFFFF

    def __init__(self, seed: int):
        self.rand_cnt = 255
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.seed[0] = seed & self.MASK
        self._init()

    @classmethod
    def _mix(cls, a, b, c, d, e, f, g, h):
        m = cls.MASK
        a = (a - e) & m
        f = (f ^ (h >> 9)) & m
        h = (h + a) & m
        b = (b - f) & m
        g = (g ^ ((a << 9) & m)) & m
        a = (a + b) & m
        c = (c - g) & m
        h = (h ^ (b >> 23)) & m
        b = (b + c) & m
        d = (d - h) & m
        a = (a ^ ((c << 15) & m)) & m
        c = (c + d) & m
        e = (e - a) & m
        b = (b ^ (d >> 14)) & m
        d = (d + e) & m
        f = (f - b) & m
        c = (c ^ ((e << 20) & m)) & m
        e = (e + f) & m
        g = (g - c) & m
        d = (d ^ (f >> 17)) & m
        f = (f + g) & m
        h = (h - d) & m
        e = (e ^ ((g << 14) & m)) & m
        g = (g + h) & m
        return a, b, c, d, e, f, g, h

    def _init(self):
        m = self.MASK
        golden = 0x9E3779B97F4A7C13
        a = b = c = d = e = f = g = h = golden
        for _ in range(4):
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
        for i in range(0, 256, 8):
            a = (a + self.seed[i]) & m
            b = (b + self.seed[i + 1]) & m
            c = (c + self.seed[i + 2]) & m
            d = (d + self.seed[i + 3]) & m
            e = (e + self.seed[i + 4]) & m
            f = (f + self.seed[i + 5]) & m
            g = (g + self.seed[i + 6]) & m
            h = (h + self.seed[i + 7]) & m
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]
        for i in range(0, 256, 8):
            a = (a + self.mm[i]) & m
            b = (b + self.mm[i + 1]) & m
            c = (c + self.mm[i + 2]) & m
            d = (d + self.mm[i + 3]) & m
            e = (e + self.mm[i + 4]) & m
            f = (f + self.mm[i + 5]) & m
            g = (g + self.mm[i + 6]) & m
            h = (h + self.mm[i + 7]) & m
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]
        self._isaac64()

    def _isaac64(self):
        m = self.MASK
        self.cc = (self.cc + 1) & m
        self.bb = (self.bb + self.cc) & m
        for i in range(256):
            rem = i % 4
            if rem == 0:
                self.aa = (~(self.aa ^ (self.aa << 21))) & m
            elif rem == 1:
                self.aa = (self.aa ^ (self.aa >> 5)) & m
            elif rem == 2:
                self.aa = (self.aa ^ (self.aa << 12)) & m
            else:
                self.aa = (self.aa ^ (self.aa >> 33)) & m
            self.aa = (self.aa + self.mm[(i + 128) & 0xFF]) & m
            x = self.mm[i]
            y = (self.mm[(x >> 3) & 0xFF] + self.aa + self.bb) & m
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) & 0xFF] + x) & m
            self.seed[i] = self.bb

    def next(self) -> int:
        result = self.seed[self.rand_cnt]
        if self.rand_cnt == 0:
            self._isaac64()
            self.rand_cnt = 255
        else:
            self.rand_cnt -= 1
        return result


def decrypt_channels_data(data: bytearray, key: int, enc_len: int = 131072) -> None:
    """就地解密视频号文件的前 ``enc_len`` 字节（默认 128 KiB）。"""
    if not data:
        return
    actual = min(len(data), enc_len)
    rng = Isaac64(int(key))
    for i in range(0, actual, 8):
        rnd = rng.next()
        chunk = struct.pack(">Q", rnd)
        for j in range(8):
            idx = i + j
            if idx >= actual:
                return
            data[idx] ^= chunk[j]


def _decrypt_file(src: Path, dest: Path, key: int, enc_len: int = 131072) -> None:
    with open(src, "rb") as f:
        head = bytearray(f.read(enc_len))
        decrypt_channels_data(head, key, enc_len)
        with open(dest, "wb") as out:
            out.write(head)
            shutil.copyfileobj(f, out, length=1024 * 1024)


def _format_bytes(n: int) -> str:
    if n >= 1024 * 1024 * 1024:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 * 1024:
        return f"{n / (1024 ** 2):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def download_http_video(
    video_url: str,
    dest: Path,
    decode_key: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout: int = 60,
) -> Path:
    """流式下载视频号 CDN 文件；有 decode_key 时解密前 128 KiB。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    session = requests.Session()
    session.trust_env = False
    downloaded = 0
    last_log = time.time()
    try:
        with session.get(
            video_url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            },
            stream=True,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if cancel_check and cancel_check():
                        raise RuntimeError("用户已停止下载")
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_log >= 5:
                        pct = f"{downloaded * 100 / total:.1f}%" if total else "?"
                        logger.info(f"视频号下载中：{_format_bytes(downloaded)} / {_format_bytes(total) if total else '?'} ({pct})")
                        last_log = time.time()
        if decode_key:
            key = int(str(decode_key), 0) if str(decode_key).lower().startswith(("0x",)) else int(str(decode_key))
            enc = dest.with_suffix(dest.suffix + ".enc")
            part.replace(enc)
            _decrypt_file(enc, dest, key)
            try:
                enc.unlink()
            except OSError:
                pass
            logger.info("视频号视频已解密（前 128 KiB）")
        else:
            part.replace(dest)
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    finally:
        session.close()
    logger.info(f"视频号视频下载完成：{dest} ({_format_bytes(dest.stat().st_size)})")
    return dest


def extract_audio(video_path: Path, audio_path: Optional[Path] = None) -> Path:
    """用 ffmpeg 抽 64k mp3（与 B 站下载器保持同一转写输入规格）。"""
    video_path = Path(video_path)
    audio_path = Path(audio_path) if audio_path else video_path.with_suffix(".mp3")
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return audio_path
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3lame", "-ab", "64k",
        str(audio_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600, **no_window_kwargs()
    )
    if result.returncode != 0 or not audio_path.exists():
        raise RuntimeError(f"ffmpeg 抽取音频失败：{(result.stderr or '')[-300:]}")
    return audio_path


def probe_duration(video_path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
            ],
            capture_output=True, text=True, timeout=30, **no_window_kwargs()
        )
        return float((result.stdout or "").strip() or 0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 公众号视频：真实浏览器会话
# ---------------------------------------------------------------------------

def _launch_edge(pw):
    """优先系统 Edge，其次 Chrome；都不存在时给出可执行的提示。"""
    errors = []
    for channel in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(
                channel=channel,
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required",
                    "--mute-audio",
                ],
            )
        except Exception as e:
            errors.append(f"{channel}: {e}")
    raise RuntimeError(
        "未找到 Microsoft Edge 或 Google Chrome，无法下载公众号视频。"
        "请先安装 Edge/Chrome 后重试。"
    )


def _capture_article_video(page, wait_seconds: int = 25) -> str:
    """打开文章后尽可能触发视频播放，返回真实 <video> 地址。"""
    deadline = time.time() + wait_seconds
    clicked = False
    while time.time() < deadline:
        try:
            srcs = page.evaluate(
                """() => Array.from(document.querySelectorAll('video'))
                    .map(v => ({src: v.currentSrc || v.src || '', duration: v.duration || 0}))
                    .filter(v => v.src)"""
            )
        except Exception:
            srcs = []
        if srcs:
            srcs.sort(key=lambda x: x.get("duration") or 0, reverse=True)
            return srcs[0]["src"]
        if not clicked:
            for selector in ("iframe.video_iframe", ".js_img_placeholder", "text=观看视频", "video"):
                try:
                    el = page.query_selector(selector)
                    if el:
                        el.click(timeout=2000, force=True)
                        clicked = True
                        break
                except Exception:
                    continue
        # 逐段滚动触发懒加载
        try:
            page.evaluate("window.scrollBy(0, Math.max(300, window.innerHeight * 0.8))")
        except Exception:
            pass
        time.sleep(1)
    return ""


def probe_article_video_via_browser(article_url: str) -> Dict:
    """诊断用：只打开文章并捕获视频地址，不下载。返回可播放性与域名。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "缺少 Playwright 运行时，无法下载公众号视频；请重新安装/构建后端。"
        ) from e
    with sync_playwright() as pw:
        browser = _launch_edge(pw)
        context = browser.new_context(
            user_agent=BROWSER_UA,
            locale="zh-CN",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            src = _capture_article_video(page, wait_seconds=20)
            host = urlparse(src).hostname if src else ""
            return {"playable": bool(src), "src_host": host or "", "src_len": len(src)}
        finally:
            try:
                context.close()
            finally:
                browser.close()


def download_article_video_via_browser(article_url: str, dest: Path, cancel_check=None) -> Path:
    """用系统 Edge 的真实会话打开文章并分片下载视频。

    腾讯 mpvideo CDN 只认浏览器会话（纯 requests 固定 403），所以这里用
    Playwright 同源 context 的 request 按 Range 抓取，避免把整段视频读进内存。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "缺少 Playwright 运行时，无法下载公众号视频；请重新安装/构建后端。"
        ) from e

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch_edge(pw)
        context = browser.new_context(
            user_agent=BROWSER_UA,
            locale="zh-CN",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            src = _capture_article_video(page)
            if not src:
                raise RuntimeError(
                    "没有在文章里找到可播放的视频。若文章内嵌的是视频号内容，"
                    "请改用「视频号」平台并粘贴视频号分享链接。"
                )
            if cancel_check and cancel_check():
                raise RuntimeError("用户已停止下载")
            logger.info(f"已捕获公众号视频地址（{len(src)} 字符），开始分片下载")

            api = context.request
            probe = api.get(
                src,
                headers={"Referer": article_url, "Range": "bytes=0-0"},
                timeout=60000,
            )
            if probe.status not in (200, 206):
                raise RuntimeError(f"视频 CDN 拒绝了浏览器会话 (HTTP {probe.status})")
            content_range = probe.headers.get("content-range") or ""
            total = 0
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = 0
            if not total:
                total = int(probe.headers.get("content-length", "0") or 0)
            if probe.status == 200 and not content_range:
                # 服务端忽略 Range，一次给了全量
                dest.write_bytes(probe.body())
                logger.info(f"公众号视频下载完成（整包）：{dest}")
                return dest

            chunk_size = 8 * 1024 * 1024
            part = dest.with_suffix(dest.suffix + ".part")
            written = 0
            with open(part, "wb") as f:
                start = 0
                while True:
                    if cancel_check and cancel_check():
                        raise RuntimeError("用户已停止下载")
                    end = start + chunk_size - 1
                    if total and start >= total:
                        break
                    resp = api.get(
                        src,
                        headers={
                            "Referer": article_url,
                            "Range": f"bytes={start}-{end}",
                        },
                        timeout=120000,
                    )
                    if resp.status not in (200, 206):
                        raise RuntimeError(f"视频分片下载失败 (HTTP {resp.status})")
                    body = resp.body()
                    if not body:
                        break
                    f.write(body)
                    written += len(body)
                    start += len(body)
                    if total and written % (32 * 1024 * 1024) < chunk_size:
                        logger.info(f"公众号视频下载中：{_format_bytes(written)} / {_format_bytes(total)}")
                    if not total and len(body) < chunk_size:
                        break
            part.replace(dest)
            logger.info(f"公众号视频下载完成：{dest} ({_format_bytes(written)})")
            return dest
        finally:
            try:
                context.close()
            finally:
                browser.close()


def default_media_dir() -> Path:
    root = get_data_dir()
    if not root:
        root = os.getcwd()
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p

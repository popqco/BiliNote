"""小红书笔记详情 API 客户端。

现状（2026-10-02 实测）：
- venv 自带 yt-dlp 2026.8.19 的 XiaoHongShuIE 已失效：匿名 GET
  `www.xiaohongshu.com/explore/<id>` 返回的 `window.__INITIAL_STATE__`
  里 `note.noteDetailMap` 为空（`{}`），页面标题是「你访问的页面不见了」，
  extractor 报 `No video formats found`。
- 小红书 Web API（`edith.xiaohongshu.com/api/sns/web/v1/feed`）需要登录
  cookie（`a1`/`web_session`）+ `x-s`/`x-t`/`x-s-common` 请求签名，
  签名算法为前端 JS（Web 端 `__sign`），本仓库不自带 JS 运行时，
  故签名由用户浏览器里的 cookie 派生不出——必须用户在「下载器配置」里
  填入已登录的 Cookie。

本模块只做「拿着用户 cookie 调官方 feed API 取直链」这一件事，
不实现签名算法：一期用 cookie 直调（部分低风控期可用），签名失败时
把服务端原文透出，引导用户更新 cookie / 稍后重试。
"""
import re
from typing import Optional

import requests

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

XIAOHONGSHU_API_BASE = "https://edith.xiaohongshu.com"

HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.xiaohongshu.com",
    "Pragma": "no-cache",
    "Referer": "https://www.xiaohongshu.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
}

cfm = CookieConfigManager()


class XiaohongshuError(Exception):
    """小红书下载链路异常（基类，便于调用方统一捕获转 NoteError）。"""


class XiaohongshuAuthError(XiaohongshuError):
    """cookie 缺失/过期：提示用户去「下载器配置」更新小红书 Cookie。"""


class XiaohongshuImageNoteError(XiaohongshuError):
    """命中图文笔记：无视频流，明确抛给用户而非静默失败。"""


def extract_note_id(url: str) -> Optional[str]:
    """从小红书链接中提取笔记 ID（24 位 hex）。

    支持形态：
    - `https://www.xiaohongshu.com/explore/<id>[?xsec_token=...][&xsec_source=...]`
    - `https://www.xiaohongshu.com/discovery/item/<id>[?...]`
    - 短链 `https://xhslink.com/xxx`（先 follow redirect 再提取）

    :return: 笔记 ID，提不到返回 None
    """
    text = url or ""
    # 先处理短链跳转（参考 douyin_downloader.find_url / extract_video_id）
    found = re.findall(
        r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
        text,
    )
    candidate = found[0] if found else text
    if "xhslink.com" in candidate:
        try:
            resp = requests.head(candidate, allow_redirects=True, timeout=15)
            candidate = resp.url or candidate
        except Exception as e:
            logger.warning(f"小红书短链解析失败: {e}")
            return None
    # 标准形态：/explore/<id> 或 /discovery/item/<id>
    match = re.search(r"(?:/explore/|/discovery/item/)([\da-fA-F]{24})", candidate)
    if match:
        return match.group(1).lower()
    # 兜底：URL 里 xsec_token 携带的 note id 场景不做猜测，直接返回 None
    return None


def extract_xsec_token(url: str) -> str:
    """从链接查询参数里提取 xsec_token（feed API 需要它定位笔记）。"""
    match = re.search(r"[?&]xsec_token=([^&]+)", url or "")
    return match.group(1) if match else ""


class Xiaohongshu:
    """小红书 feed API 客户端：查笔记详情 → 取出视频直链。"""

    def __init__(self):
        self.header = HEADERS.copy()
        self.cookie: Optional[str] = None

    def _ensure_cookie(self) -> str:
        cookie = cfm.get("xiaohongshu")
        if not cookie:
            raise XiaohongshuAuthError(
                "未配置小红书 Cookie：请到「设置 → 下载器配置 → 小红书」填入已登录的 Cookie 后重试"
            )
        self.cookie = cookie.strip()
        self.header["Cookie"] = self.cookie
        return self.cookie

    def fetch_note_card(
        self, note_id: str, xsec_token: str = "", xsec_source: str = "pc_feed"
    ) -> dict:
        """调 `POST /api/sns/web/v1/feed` 取笔记卡片。

        :raises XiaohongshuAuthError: cookie 缺失/被服务端判未登录
        :raises XiaohongshuError: 网络异常或服务端报错（含签名风控码）
        """
        self._ensure_cookie()
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
        }
        if xsec_token:
            payload["xsec_token"] = xsec_token
        if xsec_source:
            payload["xsec_source"] = xsec_source
        try:
            resp = requests.post(
                f"{XIAOHONGSHU_API_BASE}/api/sns/web/v1/feed",
                headers=self.header,
                json=payload,
                timeout=20,
            )
        except requests.RequestException as e:
            raise XiaohongshuError(f"小红书笔记详情请求失败：{e}")
        try:
            data = resp.json()
        except ValueError:
            raise XiaohongshuError(
                f"小红书接口返回非 JSON（HTTP {resp.status_code}），可能是风控拦截，建议更新 Cookie 后重试"
            )
        if not data.get("success", False) or data.get("code") not in (0, None):
            msg = data.get("msg") or data.get("message") or "未知错误"
            code = data.get("code")
            # 未登录/签名风控类错误码统一转成 Auth 错误，引导用户更新 cookie
            if code in (-2, 300012, 300013, 300014, 300016) or "登录" in str(msg):
                raise XiaohongshuAuthError(
                    f"小红书登录态失效（code={code}）：{msg}，请更新「下载器配置 → 小红书」Cookie 后重试"
                )
            raise XiaohongshuError(f"小红书接口报错（code={code}）：{msg}")
        items = data.get("data", {}).get("items") or []
        if not items:
            raise XiaohongshuError("小红书未返回笔记数据（笔记可能已删除或仅粉丝可见）")
        card = items[0].get("note_card") or {}
        if not card:
            raise XiaohongshuError("小红书笔记数据为空")
        return card

    def run(self, url: str) -> dict:
        """入口：链接 → 笔记卡片 dict（含 type/video/imageList 等字段）。"""
        note_id = extract_note_id(url)
        if not note_id:
            raise XiaohongshuError(
                "小红书链接解析失败：仅支持 `xiaohongshu.com/explore/<id>` / "
                "`discovery/item/<id>` / `xhslink.com` 短链"
            )
        card = self.fetch_note_card(note_id, extract_xsec_token(url))
        return card


def pick_video_url(card: dict) -> Optional[str]:
    """从笔记卡片中挑出可下载的视频直链；图文笔记返回 None。

    路径兼容 yt-dlp XiaoHongShuIE 的解析顺序：
    `video.media.stream[*].{masterUrl,backupUrls[*]}`，取第一条可用 URL。
    """
    video = (card or {}).get("video") or {}
    media = video.get("media") or {}
    stream = media.get("stream") or {}
    # stream 可能是 {清晰度: {...}} 的 dict，也可能是 list
    nodes = stream.values() if isinstance(stream, dict) else stream
    for node in nodes if isinstance(nodes, list) else list(nodes):
        if not isinstance(node, dict):
            continue
        master = node.get("masterUrl")
        if master:
            return master
        for backup in node.get("backupUrls") or []:
            if backup:
                return backup
    return None


def is_video_note(card: dict) -> bool:
    """`type == "video"` 即视频笔记；`normal` 为图文。"""
    return (card or {}).get("type") == "video"


def note_meta(card: dict, note_id: str) -> dict:
    """从卡片提取标题/封面/作者/tags，供 AudioDownloadResult 组装。"""
    user = (card or {}).get("user") or {}
    image_list = (card or {}).get("imageList") or []
    cover = ""
    if image_list and isinstance(image_list[0], dict):
        cover = (
            image_list[0].get("urlDefault")
            or image_list[0].get("urlPre")
            or image_list[0].get("url")
            or ""
        )
    title = (card or {}).get("title") or (card or {}).get("desc") or note_id
    tags = [
        t.get("name")
        for t in ((card or {}).get("tagList") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    duration = 0
    video = (card or {}).get("video") or {}
    media = video.get("media") or {}
    stream = media.get("stream") or {}
    nodes = stream.values() if isinstance(stream, dict) else (stream or [])
    for node in nodes if isinstance(nodes, list) else list(nodes):
        if isinstance(node, dict) and node.get("duration"):
            try:
                duration = float(node["duration"]) / 1000.0
            except (TypeError, ValueError):
                duration = 0
            break
    return {
        "video_id": note_id,
        "title": title,
        "cover_url": cover,
        "duration": duration,
        "uploader": user.get("nickname") or user.get("nickName") or "",
        "tags": tags,
        "desc": (card or {}).get("desc") or "",
    }

"""快速获取视频元信息（标题/封面/时长），不下载音视频。

用途：任务还在排队时，前端就能在生成历史里显示标题与封面，而不是等到
任务成功（或失败）后才有一个「未命名笔记」；同时也支撑提交去重展示。

路径：
1. B 站：优先直连 api.bilibili.com/x/web-interface/view（~300ms，复用下载器
   已配置的 SESSDATA cookie）；
2. 兜底：yt-dlp 元数据模式（extract_info(download=False)）。
任何失败都静默返回 None——元信息拿不到不能影响主流程。
"""
from typing import Optional

import requests

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id, normalize_video_url

logger = get_logger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _bilibili_api_meta(bvid: str) -> Optional[dict]:
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
    cookie = CookieConfigManager().get("bilibili") or ""
    if cookie:
        headers["Cookie"] = cookie
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers=headers,
            timeout=6,
        )
        data = resp.json()
    except Exception as e:
        logger.warning(f"video_meta API 请求失败: {e}")
        return None
    if data.get("code") != 0:
        logger.warning(f"video_meta API 返回错误: code={data.get('code')}, msg={data.get('message')}")
        return None
    d = data.get("data") or {}
    return {
        "video_id": d.get("bvid") or bvid,
        "title": d.get("title"),
        "cover_url": d.get("pic"),
        "duration": d.get("duration"),
        "platform": "bilibili",
    }


def _ytdlp_meta(url: str, platform: str) -> Optional[dict]:
    try:
        import yt_dlp
    except Exception:
        return None
    opts = {"quiet": True, "skip_download": True, "noplaylist": True, "no_warnings": True}
    if platform == "bilibili":
        # B 站 CDN 走直连（与下载器一致，见 bilibili_downloader._resolve_proxy_opt）
        from app.downloaders.bilibili_downloader import _resolve_proxy_opt
        opts["proxy"] = _resolve_proxy_opt()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"video_meta yt-dlp 兜底失败: {e}")
        return None
    if not info:
        return None
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "cover_url": info.get("thumbnail"),
        "duration": info.get("duration"),
        "platform": platform,
    }


def fetch_video_meta(url: str, platform: str = "bilibili") -> Optional[dict]:
    """返回 {video_id,title,cover_url,duration,platform}；失败返回 None。"""
    if platform == "bilibili":
        url = normalize_video_url(url)
        bvid = extract_video_id(url, platform)
        if bvid:
            meta = _bilibili_api_meta(bvid)
            if meta and meta.get("title"):
                return meta
    return _ytdlp_meta(url, platform)

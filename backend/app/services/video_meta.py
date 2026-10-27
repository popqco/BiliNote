"""快速获取视频元信息（标题/封面/时长），不下载音视频。

用途：任务还在排队时，前端就能在生成历史里显示标题与封面，而不是等到
任务成功（或失败）后才有一个「未命名笔记」；同时也支撑提交去重展示。

路径：
1. B 站：优先直连 api.bilibili.com/x/web-interface/view（~300ms，复用下载器
   已配置的 SESSDATA cookie）；
2. 兜底：yt-dlp 元数据模式（extract_info(download=False)）。
任何失败都静默返回 None——元信息拿不到不能影响主流程。
"""
import time
from typing import Optional

from app.services.cookie_manager import CookieConfigManager
from app.utils.bilibili_http import bilibili_session
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id, normalize_video_url

logger = get_logger(__name__)


def _bilibili_api_meta(bvid: str) -> Optional[dict]:
    headers = {"Referer": "https://www.bilibili.com"}
    cookie = CookieConfigManager().get("bilibili") or ""
    if cookie:
        headers["Cookie"] = cookie
    # 2026-10-24 实拍：任务提交时 DNS 短暂不可用（getaddrinfo failed），一次
    # 失败就把 audio_meta 写成全 null 并落进 _audio.json；重试时复用缓存，
    # 标题/封面永远补不回来（历史里出现「未命名笔记」）。元信息只有几百字节，
    # 对瞬时网络错误做短退避重试，仍失败再交给 yt-dlp 兜底。
    for attempt in range(3):
        try:
            resp = bilibili_session().get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
                timeout=6,
            )
            data = resp.json()
        except Exception as e:  # DNS / 连接 / 读超时
            if attempt < 2:
                logger.info(f"video_meta API 请求失败（第 {attempt + 1} 次），重试: {e}")
                time.sleep(0.8 * (attempt + 1))
                continue
            logger.warning(f"video_meta API 请求失败（已重试 {attempt + 1} 次）: {e}")
            return None
        if data.get("code") != 0:
            logger.warning(f"video_meta API 返回错误: code={data.get('code')}, msg={data.get('message')}")
            return None
        d = data.get("data") or {}
        meta = {
            "video_id": d.get("bvid") or bvid,
            "title": d.get("title"),
            "cover_url": d.get("pic"),
            "duration": d.get("duration"),
            "platform": "bilibili",
        }
        if meta.get("title"):
            return meta
        # 接口 200 但页面被删/地区限制时 data 为空：别把空壳缓存下来，
        # 让 fetch_video_meta 继续走 yt-dlp 兜底。
        logger.warning(f"video_meta API 未返回标题（bvid={bvid}）")
        return None
    return None


def _ytdlp_meta(url: str, platform: str) -> Optional[dict]:
    try:
        import yt_dlp
    except Exception:
        return None
    opts = {"quiet": True, "skip_download": True, "noplaylist": True, "no_warnings": True}
    # 输出接 app.log，不写 stdout（否则 sidecar 孤儿场景下 write_string 抛
    # OSError 22；no_warnings 挡不住 report_error → to_stderr）
    from app.utils.ydl_log import ydl_logger
    opts["logger"] = ydl_logger()
    cookiefile = None
    if platform == "bilibili":
        # B 站 CDN 走直连（与下载器一致，见 bilibili_downloader._resolve_proxy_opt）
        from app.downloaders.bilibili_downloader import _resolve_proxy_opt
        opts["proxy"] = _resolve_proxy_opt()
    else:
        # YouTube 等平台国内必须走用户配置的代理（与 youtube_downloader 一致）；
        # 之前这里不设代理 → 元信息抓取在国内直连必失败（2026-10-13 排队卡片
        # 「未命名笔记」的元信息补偿依赖此路径）
        from app.services.proxy_config_manager import ProxyConfigManager
        proxy = ProxyConfigManager().get_proxy_url()
        if proxy:
            opts["proxy"] = proxy
        if platform == "youtube":
            # 年龄限制视频不带登录 Cookie 连元信息都拿不到（排队卡片会显示「未命名」）
            from app.downloaders.youtube_downloader import apply_youtube_cookie, apply_youtube_extras
            apply_youtube_extras(opts)
            cookiefile = apply_youtube_cookie(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"video_meta yt-dlp 兜底失败: {e}")
        return None
    finally:
        if cookiefile:
            import os
            try:
                os.remove(cookiefile)
            except OSError:
                pass
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
    if platform == "xiaohongshu":
        # yt-dlp 的 XiaoHongShuIE 已失效（匿名拉 explore 页 noteDetailMap 为空），
        # 走自研 feed API（需用户已配小红书 Cookie）；失败静默返回 None。
        meta = _xiaohongshu_api_meta(url)
        if meta and meta.get("title"):
            return meta
        return None
    if platform == "wechat_article":
        from app.downloaders.wechat_common import fetch_wechat_article_meta
        return fetch_wechat_article_meta(url)
    if platform == "wechat_channels":
        # 匿名预览接口即可拿标题/作者/封面（播放地址仍需元宝登录态）。
        from app.downloaders.wechat_common import fetch_wechat_channels_meta
        return fetch_wechat_channels_meta(url)
    return _ytdlp_meta(url, platform)


def _xiaohongshu_api_meta(url: str) -> Optional[dict]:
    try:
        from app.downloaders.xiaohongshu_helper.xiaohongshu import (
            extract_note_id,
            extract_xsec_token,
            Xiaohongshu,
            note_meta,
        )
    except Exception as e:
        logger.warning(f"video_meta 小红书 helper 导入失败: {e}")
        return None
    note_id = extract_note_id(url)
    if not note_id:
        return None
    try:
        card = Xiaohongshu().fetch_note_card(note_id, extract_xsec_token(url))
    except Exception as e:
        logger.warning(f"video_meta 小红书 feed API 失败: {e}")
        return None
    meta = note_meta(card, note_id)
    return {
        "video_id": meta["video_id"],
        "title": meta["title"],
        "cover_url": meta["cover_url"] or None,
        "duration": meta["duration"],
        "platform": "xiaohongshu",
    }

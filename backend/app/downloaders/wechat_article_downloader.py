"""微信公众号文章视频下载器。

文章正文里的视频是腾讯 mpvideo 直链，纯 HTTP 请求会被 CDN 403；这里用
Playwright + 系统 Edge 的真实浏览器会话打开文章、触发播放，再按 Range
分片下载。公开文章不需要登录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.downloaders.wechat_common import (
    default_media_dir,
    download_article_video_via_browser,
    extract_audio,
    fetch_article_html,
    probe_duration,
)
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


class WechatArticleDownloader(Downloader):
    def __init__(self):
        super().__init__()
        self._meta = None
        self._video_path: Optional[Path] = None

    def _prepare(self, video_url: str) -> dict:
        if self._meta is None:
            meta = fetch_article_html(str(video_url))
            self._meta = meta
            logger.info(
                f"公众号文章解析完成：{meta.get('title')} "
                f"(直链 {len(meta.get('videos') or [])} 条)"
            )
        return self._meta

    def _dest_for(self, output_dir: Optional[str]) -> Path:
        meta = self._meta or {}
        folder = Path(output_dir) if output_dir else Path(default_media_dir())
        folder.mkdir(parents=True, exist_ok=True)
        vid = str(meta.get("video_id") or "wechat_article")
        safe = "".join(c for c in vid if c.isalnum() or c in "-_")[:80] or "wechat_article"
        return folder / f"wechat_article_{safe}.mp4"

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        meta = self._prepare(str(video_url))
        title = meta.get("title") or "微信公众号文章"
        duration = float(meta.get("duration") or 0)
        cover = meta.get("cover_url")
        vid = meta.get("video_id")
        raw = {
            "uploader": meta.get("uploader") or "微信公众号",
            "webpage_url": meta.get("article_url") or str(video_url),
        }
        if skip_download:
            return AudioDownloadResult(
                file_path=None,
                title=title,
                duration=duration,
                cover_url=cover,
                platform="wechat_article",
                video_id=vid,
                raw_info=raw,
                video_path=None,
            )

        mp4 = self.download_video(str(video_url), output_dir)
        mp3 = extract_audio(mp4)
        if not duration:
            duration = probe_duration(mp4)
        return AudioDownloadResult(
            file_path=str(mp3),
            title=title,
            duration=duration,
            cover_url=cover,
            platform="wechat_article",
            video_id=vid,
            raw_info=raw,
            video_path=str(mp4),
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        self._prepare(str(video_url))
        if self._video_path and self._video_path.exists():
            return str(self._video_path)
        dest = self._dest_for(output_dir)
        if dest.exists() and dest.stat().st_size > 0:
            self._video_path = dest
            return str(dest)
        download_article_video_via_browser(
            (self._meta or {}).get("article_url") or str(video_url),
            dest,
            cancel_check=self.cancel_check,
        )
        self._video_path = dest
        return str(dest)

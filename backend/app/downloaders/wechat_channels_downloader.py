"""微信视频号下载器。

匿名接口只能拿到标题/封面；要拿播放地址必须有腾讯元宝登录态（设置页
扫码登录或粘贴 Cookie）。拿到 videoUrl 后直接走腾讯 CDN 下载，若带
decodeKey 则用 ISAAC64 解密前 128 KiB。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.downloaders.wechat_common import (
    default_media_dir,
    download_http_video,
    extract_audio,
    fetch_channels_public_meta,
    probe_duration,
    resolve_channels_with_yuanbao,
)
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)


class WechatChannelsDownloader(Downloader):
    def __init__(self):
        super().__init__()
        self._cookies = CookieConfigManager()
        self._meta = None
        self._video_path: Optional[Path] = None
        self._resolve_error: Optional[str] = None

    def _resolve(self, video_url: str) -> dict:
        if self._meta is not None:
            return self._meta
        sid_url = str(video_url)
        meta = fetch_channels_public_meta(sid_url)
        cookie = self._cookies.get("wechat_channels")
        if cookie:
            try:
                rich = resolve_channels_with_yuanbao(sid_url, cookie)
                meta.update({k: v for k, v in rich.items() if v not in (None, "", 0, [], {})})
                logger.info(f"视频号已获取播放地址：{meta.get('title')}")
            except Exception as e:
                self._resolve_error = str(e)
                logger.warning(f"视频号元宝解析失败（保留公开元信息）：{e}")
        else:
            self._resolve_error = (
                "视频号视频需要腾讯元宝登录态：请到「设置 → 下载器配置 → 视频号」"
                "点击「扫码登录元宝」，或粘贴元宝 Cookie 后重试。"
            )
        self._meta = meta
        return meta

    def _dest_for(self, output_dir: Optional[str]) -> Path:
        meta = self._meta or {}
        folder = Path(output_dir) if output_dir else Path(default_media_dir())
        folder.mkdir(parents=True, exist_ok=True)
        sid = str(meta.get("video_id") or "wechat_channels")
        safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:80] or "wechat_channels"
        return folder / f"wechat_channels_{safe}.mp4"

    def _ensure_video(self, video_url: str, output_dir: Optional[str] = None) -> Path:
        meta = self._resolve(video_url)
        if self._video_path and self._video_path.exists():
            return self._video_path
        dest = self._dest_for(output_dir)
        if dest.exists() and dest.stat().st_size > 0:
            self._video_path = dest
            return dest
        play_url = meta.get("video_url")
        if not play_url:
            raise RuntimeError(self._resolve_error or "未能解析出视频号播放地址")
        download_http_video(
            play_url,
            dest,
            decode_key=str(meta.get("decode_key") or ""),
            cancel_check=self.cancel_check,
        )
        self._video_path = dest
        return dest

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        meta = self._resolve(str(video_url))
        title = meta.get("title") or "视频号视频"
        cover = meta.get("cover_url")
        vid = meta.get("video_id")
        raw = {
            "uploader": meta.get("uploader") or "视频号",
            "webpage_url": meta.get("webpage_url") or str(video_url),
            "description": meta.get("description") or "",
        }
        duration = float(meta.get("duration") or 0)
        if skip_download:
            return AudioDownloadResult(
                file_path=None,
                title=title,
                duration=duration,
                cover_url=cover,
                platform="wechat_channels",
                video_id=vid,
                raw_info=raw,
                video_path=None,
            )

        mp4 = self._ensure_video(str(video_url), output_dir)
        mp3 = extract_audio(mp4)
        if not duration:
            duration = probe_duration(mp4)
        return AudioDownloadResult(
            file_path=str(mp3),
            title=title,
            duration=duration,
            cover_url=cover,
            platform="wechat_channels",
            video_id=vid,
            raw_info=raw,
            video_path=str(mp4),
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        return str(self._ensure_video(str(video_url), output_dir))

"""小红书下载器：feed API 取视频直链 → 下 mp4 → ffmpeg 转 mp3。

链路说明：
- yt-dlp 的 XiaoHongShuIE 已失效（2026-10-02 实测匿名拉 explore 页
  `noteDetailMap` 为空），故不走 yt-dlp，自研 feed API 链路
  （见 `xiaohongshu_helper/xiaohongshu.py`）。
- 小红书视频流是音画合一的 mp4，没有独立音轨：`download()` 先落 mp4
  再经 ffmpeg 转 mp3（同 kuaishou_downloader 做法）。
- 小红书无字幕接口：`download_subtitles()` 直接返回 None，走 whisper 兜底。
- 图文笔记（`type != "video"` / 无视频流）抛 XiaohongshuImageNoteError，
  上层经 `_format_error` 落到任务状态，前端 toast 向用户明示。
"""
import os
import subprocess
from abc import ABC
from typing import Optional, Union

import requests

from app.downloaders.base import Downloader
from app.downloaders.xiaohongshu_helper.xiaohongshu import (
    Xiaohongshu,
    XiaohongshuImageNoteError,
    extract_note_id,
    is_video_note,
    note_meta,
    pick_video_url,
)
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir

logger = get_logger(__name__)


class XiaohongshuDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        self._api = Xiaohongshu()

    def _resolve_output_dir(self, output_dir: Union[str, None]) -> str:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    @staticmethod
    def _convert_to_mp3(mp4_path: str, mp3_path: str) -> None:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp4_path, "-vn", "-acodec", "libmp3lame", mp3_path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            raise Exception("ffmpeg 转换 MP3 失败")
        except FileNotFoundError:
            raise Exception("未找到 ffmpeg：小红书视频转音频需要 ffmpeg，请先安装")

    def _fetch_card(self, video_url: str) -> tuple[dict, str]:
        """拉笔记卡片 + 校验视频形态，返回 (card, note_id)。"""
        note_id = extract_note_id(video_url)
        if not note_id:
            # 短链已在 helper 内 follow；到这里还提不到就是格式不支持
            raise XiaohongshuImageNoteError(
                "小红书链接解析失败：仅支持 xiaohongshu.com/explore/<id>、"
                "discovery/item/<id> 与 xhslink.com 分享短链"
            )
        card = self._api.run(video_url)
        if not is_video_note(card):
            raise XiaohongshuImageNoteError(
                "该小红书笔记为图文形态，暂只支持视频笔记：请换一条视频笔记链接重试"
            )
        stream_url = pick_video_url(card)
        if not stream_url:
            raise XiaohongshuImageNoteError(
                "该小红书笔记暂无可下载的视频流（可能为图文或已失效），暂只支持视频笔记"
            )
        return card, note_id

    @staticmethod
    def _download_file(url: str, dest: str, referer: str = "https://www.xiaohongshu.com/") -> None:
        resp = requests.get(
            url,
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": referer,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise Exception(f"小红书视频下载失败: HTTP {resp.status_code}")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

    def _build_result(
        self,
        card: dict,
        note_id: str,
        file_path: Optional[str],
        video_path: Optional[str],
    ) -> AudioDownloadResult:
        meta = note_meta(card, note_id)
        return AudioDownloadResult(
            file_path=file_path,
            title=meta["title"],
            duration=meta["duration"],
            cover_url=meta["cover_url"] or None,
            platform="xiaohongshu",
            video_id=note_id,
            raw_info={
                "tags": ",".join(meta["tags"]),
                "desc": meta["desc"],
                "uploader": meta["uploader"],
            },
            video_path=video_path,
        )

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        output_dir = self._resolve_output_dir(output_dir)
        card, note_id = self._fetch_card(video_url)

        if skip_download:
            # 只取元信息：转写已由他处提供/或仅做展示，音轨无下游消费者
            # （与 bilibili/youtube 的 skip_download 语义一致）。
            logger.info(f"小红书仅提取元信息: note_id={note_id}")
            return self._build_result(card, note_id, file_path=None, video_path=None)

        mp4_path = os.path.join(output_dir, f"{note_id}.mp4")
        mp3_path = os.path.join(output_dir, f"{note_id}.mp3")
        if os.path.exists(mp3_path):
            logger.info(f"[已存在] 跳过下载: {mp3_path}")
            video_path = mp4_path if os.path.exists(mp4_path) else None
            return self._build_result(card, note_id, file_path=mp3_path, video_path=video_path)

        stream_url = pick_video_url(card)
        logger.info(f"正在下载小红书视频: note_id={note_id}")
        self._download_file(stream_url, mp4_path)
        self._convert_to_mp3(mp4_path, mp3_path)
        return self._build_result(card, note_id, file_path=mp3_path, video_path=mp4_path)

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        output_dir = self._resolve_output_dir(output_dir)
        note_id = extract_note_id(video_url) or "xiaohongshu"
        video_path = os.path.join(output_dir, f"{note_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        card, resolved_id = self._fetch_card(video_url)
        video_path = os.path.join(output_dir, f"{resolved_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        stream_url = pick_video_url(card)
        logger.info(f"正在下载小红书视频（mp4）: note_id={resolved_id}")
        self._download_file(stream_url, video_path)
        return video_path

    # 小红书无字幕接口：返回 None 即可走 whisper 兜底（基类默认行为，
    # 这里显式声明以表明是深思熟虑而非遗漏）。
    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: list = None):
        return None

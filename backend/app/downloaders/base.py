import enum

from abc import ABC, abstractmethod
from typing import Callable, Optional, Union

from app.enmus.note_enums import DownloadQuality
from app.exceptions.task_cancel import TaskCancelled
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.utils.ydl_log import ydl_logger
from os import getenv
QUALITY_MAP = {
    "fast": "32",
    "medium": "64",
    "slow": "128"
}

# yt-dlp 的 `retries` 默认值(10)是命令行参数解析器给的，Python API 不套用它：
# 不显式设置时 HttpFD 拿到的是 `self.params.get('retries')` == None，而
# `RetryManager.__init__` 做的是 `self.retries = _retries or 0`——也就是
# 一次都不重试。任何一次网络抖动（例如 B 站 CDN
# upos-sz-mirror*.bilivideo.com 读超时）都会让整个笔记任务直接失败。
#
# 这里的值偏保守：笔记任务是用户在前台等的，重试太多不如早点失败让用户重来。
#
# `logger`（2026-10-16）：yt-dlp 输出不再写 stdout/stderr 而是进 app.log。
# sidecar 孤儿的 stdout 管道会失效（app 被强杀后），届时 yt-dlp 的
# to_screen 写失败直接抛 OSError 22 打崩下载；挂 logger 后该路径整体消失。
# 进度/调试走 DEBUG（默认不落盘），警告/错误按级别进 app.log。
_YDL_LOGGER = ydl_logger()
YDL_RETRY_OPTS = {
    "retries": 3,
    "fragment_retries": 3,
    "socket_timeout": 30,
    "logger": _YDL_LOGGER,
}


class Downloader(ABC):
    # 取消谓词（由 NoteGenerator 注入）：返回 True 表示用户已请求停止。
    # yt-dlp 系下载器把它挂进进度回调，正在跑的下载立即中断；
    # 不支持中断的平台（直连 HTTP）至少在下载前后被 note.py 的检查点覆盖。
    cancel_check: Optional[Callable[[], bool]] = None

    def __init__(self):
        #TODO 需要修改为可配置
        self.quality = QUALITY_MAP.get('fast')
        self.cache_data=getenv('DATA_DIR')

    def _progress_hook_with_cancel(self):
        """yt-dlp 进度回调：命中取消即抛 TaskCancelled 中断下载。

        从 progress hook 抛出的异常会原样穿出 yt-dlp 的 download/extract_info
        （不落进它的 DownloadError 重试分类，见 _ydl_extract_download 只捕
        DownloadError），所以取消不会被当成「瞬态网络错误」反复重试。
        """
        cancel_check = self.cancel_check

        def _hook(_status: dict) -> None:
            if cancel_check and cancel_check():
                raise TaskCancelled()

        return _hook

    def _ydl_opts_with_cancel(self, ydl_opts: dict) -> dict:
        """给 yt-dlp 选项挂上取消回调（未注入 cancel_check 时原样返回）。"""
        if not self.cancel_check:
            return ydl_opts
        hooks = list(ydl_opts.get("progress_hooks") or [])
        hooks.append(self._progress_hook_with_cancel())
        return {**ydl_opts, "progress_hooks": hooks}

    def cleanup(self) -> None:
        """释放本次实例持有的临时资源（默认无）。"""

    @abstractmethod
    def download(self, video_url: str, output_dir: str = None,
                 quality: DownloadQuality = "fast", need_video: Optional[bool] = False,
                 skip_download: bool = False) -> AudioDownloadResult:
        '''

        :param need_video:
        :param video_url: 资源链接
        :param output_dir: 输出路径 默认根目录data
        :param quality: 音频质量 fast | medium | slow
        :return:返回一个 AudioDownloadResult 类
        '''
        pass

    @staticmethod
    def download_video(self, video_url: str,
                       output_dir: Union[str, None] = None) -> str:
        pass

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: list = None) -> Optional[TranscriptResult]:
        '''
        尝试获取平台字幕（人工字幕或自动生成字幕）

        :param video_url: 视频链接
        :param output_dir: 输出路径
        :param langs: 优先语言列表，如 ['zh-Hans', 'zh', 'en']
        :return: TranscriptResult 或 None（无字幕时）
        '''
        return None

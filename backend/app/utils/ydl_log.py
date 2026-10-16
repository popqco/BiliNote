"""yt-dlp 输出适配器：把 yt-dlp 的全部输出接进 app.log，不再写 stdout/stderr。

为什么需要（2026-10-16）：sidecar 的 stdout/stderr 在桌面端被强杀后会变成
无效句柄，yt-dlp 的 to_screen/报告错误走 `write_string` 直接抛
`OSError: [Errno 22] Invalid argument`（回溯落在 yt_dlp/utils/_utils.py 的
write_string），下载/字幕整条链路直接失败。挂上 `logger` 参数后 yt-dlp 把
所有输出改交这里：进度/调试 → DEBUG（默认不落盘，避免刷屏），警告/错误 →
对应级别进 app.log。安全网的另一半见 safe_stdio（兜住所有非 yt-dlp 的写）。
"""

from __future__ import annotations

from app.utils.logger import get_logger


class YdlLogger:
    """yt-dlp `logger` 参数要求的适配器（debug/info/warning/error 四方法）。"""

    def __init__(self, name: str = "yt_dlp"):
        self._logger = get_logger(name)

    def debug(self, msg):
        # yt-dlp 走 logger.debug 的消息带 '[debug] ' 前缀（含进度行）
        self._logger.debug("%s", msg)

    def info(self, msg):
        self._logger.debug("%s", msg)

    def warning(self, msg):
        self._logger.warning("%s", msg)

    def error(self, msg):
        self._logger.error("%s", msg)


_shared: YdlLogger | None = None


def ydl_logger() -> YdlLogger:
    """共享单例（YDL_RETRY_OPTS 是模块级常量，构造一次即可）。"""
    global _shared
    if _shared is None:
        _shared = YdlLogger()
    return _shared

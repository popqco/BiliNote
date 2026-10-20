"""进程级 ffmpeg 并发闸门。

`VideoReader` 每个任务会并行抽帧（最多 8 个 worker），截图插入也会起 ffmpeg；
多个任务并发时子进程数量没有全局上限。这里给所有 ffmpeg 调用一个共享信号量，
默认 4（`FFMPEG_MAX_CONCURRENCY` 可覆盖），避免把 CPU/句柄打满。
"""
import os
import threading


def _max_concurrency() -> int:
    try:
        value = int(os.getenv("FFMPEG_MAX_CONCURRENCY", "4") or 4)
    except (TypeError, ValueError):
        value = 4
    return max(1, min(16, value))


ffmpeg_slots = threading.BoundedSemaphore(_max_concurrency())

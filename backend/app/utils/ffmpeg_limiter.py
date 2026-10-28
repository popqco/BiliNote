"""进程级 ffmpeg 并发闸门。

`VideoReader` 抽帧（整段解码或逐帧 seek）与截图插入都会起 ffmpeg 子进程；多个
任务并发时子进程数量没有上限。这里给所有 ffmpeg 调用一个共享信号量，避免把
CPU/句柄打满。

2026-10-28：默认值从 4 提到「min(8, 核数)」。抽帧主路径已改成每个任务一个
ffmpeg 整段解码（见 VideoReader._extract_frames_batch），4 个槽会让 8 个并发
任务排成两轮、白等一倍时间；单路解码吃不满一个核，8 路并行恰好用满 16 核。
"""
import os
import threading


def _max_concurrency() -> int:
    default = min(8, os.cpu_count() or 4)
    try:
        value = int(os.getenv("FFMPEG_MAX_CONCURRENCY", str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(16, value))


ffmpeg_slots = threading.BoundedSemaphore(_max_concurrency())

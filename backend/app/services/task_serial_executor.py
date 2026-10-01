import os
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, Optional


class ConcurrentTaskExecutor:
    """线程池并发执行任务。

    max_workers 默认 2（TASK_MAX_WORKERS 可覆盖）：下载/转写（吃本地 GPU）与
    LLM 总结（等网络）可以重叠，整批吞吐约提升 40%。同一视频的并发文件锁冲突
    （WinError 32）由 VideoTaskLocks + 提交去重消除，不靠全局串行（见 ADR-0001）。
    同时登记排队顺序，供前端展示「排队中 · 第 N 位」。
    """

    def __init__(self, max_workers: int | None = None):
        self._max_workers = max_workers or int(os.getenv("TASK_MAX_WORKERS", "2"))
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
        self._lock = threading.Lock()
        self._queued: list[str] = []           # 尚未开始执行的 task_id，按提交顺序
        self._states: Dict[str, str] = {}      # task_id -> queued | running

    def run(self, fn: Callable[..., Any], *args: Any, task_id: Optional[str] = None, **kwargs: Any) -> Any:
        if task_id:
            with self._lock:
                self._states[task_id] = "queued"
                self._queued.append(task_id)
        try:
            future: Future = self._pool.submit(self._wrapped, fn, task_id, *args, **kwargs)
            return future.result()
        except BaseException:
            if task_id:
                with self._lock:
                    self._states.pop(task_id, None)
                    if task_id in self._queued:
                        self._queued.remove(task_id)
            raise

    def _wrapped(self, fn: Callable[..., Any], task_id: Optional[str], *args: Any, **kwargs: Any) -> Any:
        if task_id:
            with self._lock:
                self._states[task_id] = "running"
                if task_id in self._queued:
                    self._queued.remove(task_id)
        try:
            return fn(*args, **kwargs)
        finally:
            if task_id:
                with self._lock:
                    self._states.pop(task_id, None)

    def queue_position(self, task_id: str) -> Optional[int]:
        """排队位次：前面还有多少个尚未开始的任务（1 起）。不在队列中返回 None。"""
        with self._lock:
            if self._states.get(task_id) == "queued" and task_id in self._queued:
                return self._queued.index(task_id) + 1
        return None

    def shutdown(self, wait: bool = True):
        self._pool.shutdown(wait=wait)


class VideoTaskLocks:
    """按 video_id 的任务互斥锁：同一视频的任务串行执行，不同视频互不阻塞。"""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


# 转写阶段全局信号量：GPU/显存只允许一个 whisper 实例并发（见 ADR-0001）。
# 下载与 LLM 总结阶段不受影响，可与转写重叠。
transcribe_semaphore = threading.Semaphore(1)

# 保持向后兼容的导出名
SerialTaskExecutor = ConcurrentTaskExecutor
task_serial_executor = ConcurrentTaskExecutor()
video_task_locks = VideoTaskLocks()

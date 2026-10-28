"""全局 LLM 流式调用闸门（2026-10-28）。

问题：任务并发 8 × 分批并发 4 = 32 条流同时打同一个网关。网关一抖，32 个批次
各自重试，退避又没抖动（都是 3s/6s 同一拍），于是同时醒来再打——一小段抖动被
放大成雪崩。2026-10-28 实测：一小时内 31 次 APIConnectionError、68 次
CompletionStalled，每条失败还会触发「降级阶梯」把所有批次重跑一遍。

两层保护：
1. 并发上限 `LLM_MAX_CONCURRENCY`（默认 8）：跨任务共享，超出的批次在本地排队，
   而不是一起打过去；
2. 熔断冷却：出现连接错误 / 静默超时后，所有**新**请求先等一小段带随机抖动的
   退避再发，把「同时重试」摊平成「错峰重试」。冷却只影响新请求，在飞的流不受
   影响，也不会因为一个批次成功就整体取消（否则兄弟批次刚熔断就被清零）。

两个值都可以用环境变量调；设 `LLM_MAX_CONCURRENCY=0` 可完全关闭闸门（回退旧行为）。
"""
import os
import random
import threading
import time
from typing import Callable, Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


class LlmGate:
    """跨任务共享的 LLM 并发闸门 + 熔断冷却。"""

    def __init__(self,
                 max_concurrency: Optional[int] = None,
                 cooldown_base: Optional[float] = None,
                 cooldown_max: Optional[float] = None):
        self.max_concurrency = _env_int("LLM_MAX_CONCURRENCY", 8) if max_concurrency is None else max_concurrency
        self._base = _env_float("LLM_COOLDOWN_BASE_SECONDS", 3.0) if cooldown_base is None else cooldown_base
        self._max = _env_float("LLM_COOLDOWN_MAX_SECONDS", 30.0) if cooldown_max is None else cooldown_max
        self._sem = threading.BoundedSemaphore(self.max_concurrency) if self.max_concurrency > 0 else None
        self._lock = threading.Lock()
        self._cool_until = 0.0
        self._fail_streak = 0

    # -- 并发 --------------------------------------------------------------
    def acquire(self, cancel_check: Optional[Callable[[], None]] = None) -> None:
        """取一个并发位；排队期间每 0.25s 检查一次取消（点停止不用等配额）。"""
        if self._sem is None:
            return
        while True:
            if self._sem.acquire(timeout=0.25):
                return
            if cancel_check:
                cancel_check()

    def release(self) -> None:
        if self._sem is None:
            return
        try:
            self._sem.release()
        except ValueError:
            # 多释放只是记账错乱，不该把任务炸掉
            pass

    # -- 熔断 --------------------------------------------------------------
    def note_failure(self) -> None:
        """记录一次上游失败：指数抬升冷却，并叠加 0.7–1.3 抖动错峰。"""
        with self._lock:
            self._fail_streak = min(self._fail_streak + 1, 6)
            delay = min(self._max, self._base * (2 ** (self._fail_streak - 1)))
            delay *= random.uniform(0.7, 1.3)
            self._cool_until = max(self._cool_until, time.monotonic() + delay)

    def note_success(self) -> None:
        """成功只衰减失败计数，不立即清冷却：同一批兄弟请求刚刚熔断时不该被
        一个成功请求放行整片新流量。冷却时间到了自然解除。"""
        with self._lock:
            if self._fail_streak:
                self._fail_streak -= 1

    def wait_cooldown(self, cancel_check: Optional[Callable[[], None]] = None) -> None:
        """新请求发出前等待全局冷却窗口结束（0.25s 粒度，可取消）。"""
        while True:
            with self._lock:
                remaining = self._cool_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))
            if cancel_check:
                cancel_check()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "max_concurrency": self.max_concurrency,
                "fail_streak": self._fail_streak,
                "cooldown_remaining": max(0.0, round(self._cool_until - time.monotonic(), 1)),
            }


llm_gate = LlmGate()

"""全局 LLM 闸门（app/gpt/llm_limiter.py）单测。

背景：任务并发 × 分批并发 = 几十条流同时打网关，一段抖动会被同步重试放大成
雪崩（2026-10-28 实测 1 小时 31 次 APIConnectionError）。闸门要保证两件事：
1. 并发位绝不超过配置值；
2. 失败后的冷却窗口真的会拦住**新**请求，但不会无限延长。
"""
import threading
import time

import pytest

from app.gpt.llm_limiter import LlmGate


def test_semaphore_caps_peak_concurrency():
    gate = LlmGate(max_concurrency=3, cooldown_base=0, cooldown_max=0)
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker():
        nonlocal active, peak
        gate.acquire()
        try:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
        finally:
            gate.release()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak <= 3
    assert gate.snapshot()["max_concurrency"] == 3


def test_zero_concurrency_disables_gate():
    """LLM_MAX_CONCURRENCY=0 是回退开关：不限制、不阻塞。"""
    gate = LlmGate(max_concurrency=0, cooldown_base=0, cooldown_max=0)
    gate.acquire()
    gate.acquire()   # 不阻塞、不报错
    gate.release()
    gate.note_failure()
    started = time.monotonic()
    gate.wait_cooldown()
    assert time.monotonic() - started < 0.5


def test_failure_opens_cooldown_and_blocks_new_requests():
    gate = LlmGate(max_concurrency=4, cooldown_base=0.4, cooldown_max=0.4)
    gate.note_failure()
    started = time.monotonic()
    gate.wait_cooldown()
    elapsed = time.monotonic() - started
    # 单次冷却 0.4s 再叠加 0.7–1.3 抖动 → 至少 0.28s
    assert elapsed >= 0.2
    assert gate.snapshot()["cooldown_remaining"] == 0


def test_success_decrements_streak_without_cancelling_cooldown():
    gate = LlmGate(max_concurrency=4, cooldown_base=10, cooldown_max=10)
    gate.note_failure()
    gate.note_failure()
    assert gate.snapshot()["fail_streak"] == 2
    gate.note_success()
    assert gate.snapshot()["fail_streak"] == 1
    # 兄弟批次刚熔断时，一个成功请求不该把整片新流量立刻放行
    assert gate.snapshot()["cooldown_remaining"] > 0


def test_wait_cooldown_honours_cancel_check():
    gate = LlmGate(max_concurrency=4, cooldown_base=5, cooldown_max=5)

    class _Cancel(Exception):
        pass

    def _cancel():
        raise _Cancel()

    gate.note_failure()
    with pytest.raises(_Cancel):
        gate.wait_cooldown(_cancel)

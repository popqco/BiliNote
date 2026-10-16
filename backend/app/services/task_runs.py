"""运行中任务的「停止」登记表（进程内）。

用户诉求（2026-10-16）：设置错了模型/参数时没法中断正在跑的生成——尤其
失败重试（转写 3 次、总结降级阶梯）期间只能干等，等完才能改设置重来。

机制：每次执行（run_note_task → NoteGenerator.generate）登记一个 TaskRun，
任务在阶段边界/重试间隙调 is_run_cancelled(run) 检查；/cancel_task 端点给
当前 run 的 cancel_event 打标，正在跑的流程在下一个检查点抛 TaskCancelled，
由 generate() 收敛为 CANCELLED 终态。

几个必须处理的并发场景（都有对应测试）：
1. 「排队中」的任务也要能停——run 在进线程池之前就登记，所以队列里的任务
   同样能被停；它在等到 worker 后第一件事就是检查取消。
2. 停了立刻重试：旧 run 可能还活着（卡在不可中断的下载/HTTP 调用里），
   register_run 会把旧 run 标为 superseded——旧 run 之后所有状态写入被
   抑制，不会把新 run 的 PENDING/进度覆盖成 CANCELLED。
3. 「点停止」与「提交/线程启动」之间的毫秒级窗口：此时没有 live run，
   request_cancel_task 落一枚「预取消」墓碑，晚到的 register_run 直接以
   已取消状态诞生（检查点立即命中）。下一次提交（重试）会 clear 墓碑，
   用户的停止只作用于它点的那一刻，不误伤新的重试。
"""

import itertools
import threading
from typing import Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_seq_counter = itertools.count(1)


class TaskRun:
    """一次任务执行的身份与取消信号（进程内）。"""

    __slots__ = ("task_id", "seq", "cancel_event", "superseded")

    def __init__(self, task_id: str, seq: int):
        self.task_id = task_id
        self.seq = seq
        self.cancel_event = threading.Event()
        self.superseded = False

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def __repr__(self) -> str:
        return (
            f"<TaskRun {self.task_id[:8]} seq={self.seq} "
            f"cancelled={self.cancelled} superseded={self.superseded}>"
        )


_lock = threading.Lock()
_runs: Dict[str, TaskRun] = {}          # task_id -> 当前活动 run
_pre_cancel: set[str] = set()           # 提交与线程启动之间到达的停止请求


def register_run(task_id: str) -> TaskRun:
    """登记一次新的执行；同 task_id 的旧 run 一律作废（superseded）。

    旧 run 作废后其状态写入被抑制（见 NoteGenerator._update_status），
    否则它会用迟到的 CANCELLED/进度把新 run 的状态覆盖掉。
    """
    with _lock:
        old = _runs.get(task_id)
        if old is not None:
            old.superseded = True
            old.cancel_event.set()
            logger.info(f"任务重复执行，旧 run 作废并请求停止: {old!r}")
        run = TaskRun(task_id, next(_seq_counter))
        _runs[task_id] = run
        if task_id in _pre_cancel:
            # 提交与线程启动之间已收到停止请求：新 run 直接以已取消状态诞生
            _pre_cancel.discard(task_id)
            run.cancel_event.set()
            logger.info(f"任务在启动前已被请求停止，立即取消: {run!r}")
        return run


def finish_run(run: Optional[TaskRun]) -> None:
    """执行收尾：仅当自己仍是当前 run 时摘除登记。"""
    if run is None:
        return
    with _lock:
        if _runs.get(run.task_id) is run:
            del _runs[run.task_id]


def current_run(task_id: str) -> Optional[TaskRun]:
    with _lock:
        return _runs.get(task_id)


def request_cancel_task(task_id: str) -> bool:
    """请求停止当前 run；返回是否有可直接停的 live run。

    没有 live run 时（例如提交刚发出、执行线程还没登记）落墓碑，
    保证晚到的 run 也会立即取消；调用方负责在磁盘状态已终态时不做墓碑
    （避免误伤未来的重试），常规路径由 clear_cancel_request 兜底。
    """
    if not task_id:
        return False
    with _lock:
        run = _runs.get(task_id)
        if run is not None:
            run.cancel_event.set()
            logger.info(f"已请求停止任务 {run!r}")
            return True
        _pre_cancel.add(task_id)
        logger.info(f"任务当前无 live run，登记预取消墓碑: {task_id[:8]}")
        return False


def clear_cancel_request(task_id: str) -> None:
    """清除墓碑：新的提交（重试）不该被上一次的停止请求误伤。"""
    if not task_id:
        return
    with _lock:
        if task_id in _pre_cancel:
            _pre_cancel.discard(task_id)
            logger.info(f"已清除预取消墓碑（新提交）: {task_id[:8]}")


def is_run_cancelled(run: Optional[TaskRun]) -> bool:
    return bool(run is not None and run.cancel_event.is_set())


def is_task_cancel_requested(task_id: str) -> bool:
    """该 task 当前是否有停止请求（含墓碑）。"""
    with _lock:
        run = _runs.get(task_id)
        if run is not None and run.cancel_event.is_set():
            return True
        return task_id in _pre_cancel


def reset_for_tests() -> None:
    """仅供测试：清空登记表。"""
    with _lock:
        _runs.clear()
        _pre_cancel.clear()

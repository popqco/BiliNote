"""运行中任务的 run 身份、取消墓碑与活跃登记（进程内）。

从 ``services/task_runs.py`` 与 ``services/note.py`` 的 `_active_tasks` 合并而来。
两者描述的是同一件事——「这个 task 现在有没有一次执行、能不能停、同视频是否
已在跑」——放在同一个 module 里才不会出现「内存说在跑、磁盘说已结束」的对账。

磁盘状态文件由 ``store.py`` 拥有；本模块只在 ``release_stale`` 时读它。
"""
import itertools
import threading
from typing import Dict, Optional

from app.enmus.task_status_enums import TERMINAL_STATUSES
from app.services.task_state.store import read_status
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


_run_lock = threading.Lock()
_runs: Dict[str, TaskRun] = {}          # task_id -> 当前活动 run
_pre_cancel: set[str] = set()           # 提交与线程启动之间到达的停止请求


def register_run(task_id: str) -> TaskRun:
    """登记一次新的执行；同 task_id 的旧 run 一律作废（superseded）。"""
    with _run_lock:
        old = _runs.get(task_id)
        if old is not None:
            old.superseded = True
            old.cancel_event.set()
            logger.info(f"任务重复执行，旧 run 作废并请求停止: {old!r}")
        run = TaskRun(task_id, next(_seq_counter))
        _runs[task_id] = run
        if task_id in _pre_cancel:
            _pre_cancel.discard(task_id)
            run.cancel_event.set()
            logger.info(f"任务在启动前已被请求停止，立即取消: {run!r}")
        return run


def finish_run(run: Optional[TaskRun]) -> None:
    """执行收尾：仅当自己仍是当前 run 时摘除登记。"""
    if run is None:
        return
    with _run_lock:
        if _runs.get(run.task_id) is run:
            del _runs[run.task_id]


def current_run(task_id: str) -> Optional[TaskRun]:
    with _run_lock:
        return _runs.get(task_id)


def request_cancel_task(task_id: str) -> bool:
    """请求停止当前 run；返回是否有可直接停的 live run。"""
    if not task_id:
        return False
    with _run_lock:
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
    with _run_lock:
        if task_id in _pre_cancel:
            _pre_cancel.discard(task_id)
            logger.info(f"已清除预取消墓碑（新提交）: {task_id[:8]}")


def is_run_cancelled(run: Optional[TaskRun]) -> bool:
    return bool(run is not None and run.cancel_event.is_set())


def is_task_cancel_requested(task_id: str) -> bool:
    """该 task 当前是否有停止请求（含墓碑）。"""
    with _run_lock:
        run = _runs.get(task_id)
        if run is not None and run.cancel_event.is_set():
            return True
        return task_id in _pre_cancel


# ---------------- 活跃任务登记（同视频互斥 / 删除保护） ----------------

_active_tasks: dict[str, dict] = {}
_active_tasks_lock = threading.Lock()


def mark_task_active(task_id: str, video_id: Optional[str] = None, token: object = None) -> None:
    if not task_id:
        return
    with _active_tasks_lock:
        _active_tasks[task_id] = {"video_id": video_id or "", "token": token}


def mark_task_done(task_id: str, token: object = None) -> None:
    if not task_id:
        return
    with _active_tasks_lock:
        info = _active_tasks.get(task_id)
        if info is None:
            return
        if token is not None and info.get("token") is not None and info["token"] is not token:
            logger.info(f"任务登记已被新的执行接管，跳过本次摘除 (task_id={task_id})")
            return
        _active_tasks.pop(task_id, None)


def is_task_active(task_id: str) -> bool:
    with _active_tasks_lock:
        return task_id in _active_tasks


def active_task_for_video(video_id: str) -> Optional[str]:
    """同视频是否已有任务在本进程排队/执行中（跨进程靠状态文件兜底）。"""
    if not video_id:
        return None
    with _active_tasks_lock:
        for tid, info in _active_tasks.items():
            if info.get("video_id") and info["video_id"] == video_id:
                return tid
    return None


def release_stale_active_task(task_id: str, root=None) -> bool:
    """内存登记 vs 磁盘终态对齐：内存仍登记但磁盘已终态 → 清僵尸并放行。"""
    if not task_id or not is_task_active(task_id):
        return False
    data = read_status(task_id, root)
    if not data:
        return False
    if data.get("status") in TERMINAL_STATUSES:
        logger.warning(
            "清理僵尸任务登记：内存仍登记但磁盘已是终态 "
            f"(task_id={task_id}, disk_status={data.get('status')})，放行重试"
        )
        mark_task_done(task_id)
        return True
    return False


def reset_for_tests() -> None:
    """仅供测试：清空 run、墓碑与活跃登记。"""
    with _run_lock:
        _runs.clear()
        _pre_cancel.clear()
    with _active_tasks_lock:
        _active_tasks.clear()

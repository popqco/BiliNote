"""兼容层：实现已移到 app.services.task_state.runs。

保留本模块一个版本，供工作树里的旧分支与测试继续 import；新代码直接用
``app.services.task_state``。
"""
from app.services.task_state.runs import (
    TaskRun,
    clear_cancel_request,
    current_run,
    finish_run,
    is_run_cancelled,
    is_task_cancel_requested,
    register_run,
    request_cancel_task,
    reset_for_tests,
)

__all__ = [
    "TaskRun",
    "clear_cancel_request",
    "current_run",
    "finish_run",
    "is_run_cancelled",
    "is_task_cancel_requested",
    "register_run",
    "request_cancel_task",
    "reset_for_tests",
]

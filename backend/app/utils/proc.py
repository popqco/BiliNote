"""Windows 子进程「不弹控制台窗口」工具（2026-10-16）。

背景：后端被以无控制台方式拉起时（例如部署脚本用 DETACHED_PROCESS），每 spawn
一次 ffmpeg 这类控制台程序，Windows 都会给子进程新分配一个控制台——Win11 默认
终端把控制台交给 WindowsTerminal 承载，表现为一个个标题为 ffmpeg.exe 的终端
窗口成群弹出（抽帧是并行批量的，一个任务几十次调用；2026-10-16 用户实拍「突然
出现一堆终端窗口」）。父进程若自带（哪怕是隐藏的）控制台，子进程会继承而不弹窗，
但不能依赖拉起方式——所有从后端 spawn 的进程都显式带 CREATE_NO_WINDOW。

用法：
    from app.utils.proc import no_window_kwargs
    subprocess.run(cmd, check=True, **no_window_kwargs())
"""

from __future__ import annotations

import subprocess
import sys

# CREATE_NO_WINDOW = 0x08000000；老版本 Python 的 subprocess 没有该常量，兜底
_CREATE_NO_WINDOW = 0x08000000


def no_window_kwargs() -> dict:
    """subprocess.run/Popen 的兼容 kwargs：Windows 带 CREATE_NO_WINDOW，其它平台空。"""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}
    return {}

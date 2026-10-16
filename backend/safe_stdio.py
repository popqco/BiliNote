"""stdout/stderr 容错代理：管道读端消失后，写操作不再打崩整个任务。

背景（2026-10-16 实拍）：BiliNoteBackend 由桌面端以管道方式拉起（stdout/stderr
转发到前端日志面板）。app 被强杀/替换部署时 sidecar 成为孤儿却继续服务，
但它的 stdout/stderr 管道读端已随 app 消失——Windows 上此后任何一次写入都会
抛 `OSError: [Errno 22] Invalid argument`：print()、yt-dlp 的 to_screen、
tqdm 进度条全中招，B 站字幕获取与音视频下载整链全灭（前端表现为「生成失败:
OSError: [Errno 22] Invalid argument」）。日志文件本身没坏，所以 app.log 里
能看到完整的 `yt_dlp ... write_string` 回溯。

install() 把 sys.stdout/stderr 包成容错代理：
- 正常可写时行为不变（开发态、带控制台运行）；
- 写失败（管道断开 / UnicodeEncodeError）不再抛异常：破管只记一条 warning，
  之后的输出降级写入 logs/stdio_fallback.log（DEBUG 级别），不刷屏、不递归；
- 代理自身日志走独立的 no-propagate logger，绝不回流到会被写坏的流上
  （否则 console handler → 破管 → 再记日志 → 无限递归）。

本模块刻意放在 backend 顶层（不 import app 包）：main.py 可以最先安装它——
实测「坏流状态下 import app 包」会在守卫装上前就崩（应用导入链里任何一次
stderr 写入都会把进程带走），所以守卫必须早于一切 app 导入。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FALLBACK_LOGGER_NAME = "bilinote.stdio_fallback"
_fallback_logger: logging.Logger | None = None


def _fallback_logger_get() -> logging.Logger:
    """独立 fallback logger：只写 logs/stdio_fallback.log，不向任何标准流输出。

    刻意不复用 app.utils.logger：它的 console_handler 挂在 sys.stdout 上，
    破管场景下会「写失败 → 记日志 → 又写失败」形成递归（见模块文档）。
    """
    global _fallback_logger
    if _fallback_logger is not None:
        return _fallback_logger
    lg = logging.getLogger(_FALLBACK_LOGGER_NAME)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if not lg.handlers:
        try:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            handler = logging.FileHandler(log_dir / "stdio_fallback.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(handler)
        except Exception:
            lg.addHandler(logging.NullHandler())
    _fallback_logger = lg
    return lg


class ResilientStream:
    """包装原始 stream；写入失败时降级为文件日志，绝不向调用方抛异常。"""

    def __init__(self, stream, name: str):
        self._stream = stream
        self._name = name  # 'stdout' / 'stderr'
        self._broken = False
        self._warned = False

    # ---- 写路径 -----------------------------------------------------------
    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if not self._broken and self._stream is not None:
            try:
                return self._stream.write(s)
            except UnicodeEncodeError:
                # 流可写但编码装不下（cp936 控制台 + emoji）：替换字符后重写
                try:
                    enc = getattr(self._stream, "encoding", None) or "utf-8"
                    return self._stream.write(s.encode(enc, "replace").decode(enc))
                except (OSError, ValueError):
                    pass
            except (OSError, ValueError):
                pass
            self._mark_broken()
        self._fallback(s)
        return len(s)

    def _mark_broken(self):
        self._broken = True
        if self._warned:
            return
        self._warned = True
        msg = (
            f"{self._name} 不可写（管道已断开/句柄无效）——后续输出降级为 "
            f"logs/stdio_fallback.log 的 DEBUG 日志，不影响任务执行。"
            f"典型成因：桌面端被强杀后 sidecar 成孤儿继续服务。"
        )
        try:
            _fallback_logger_get().warning("%s", msg)
        except Exception:
            pass
        # 镜像一份到 app.log（best-effort；若 logger 自己又写坏流，代理已 broken，
        # 会走上面的 fallback 分支，不会递归）
        try:
            from app.utils.logger import get_logger

            get_logger(__name__).warning(msg)
        except Exception:
            pass

    def _fallback(self, s):
        if not str(s).strip():
            return
        try:
            _fallback_logger_get().debug("[%s] %s", self._name, str(s).rstrip("\n"))
        except Exception:
            pass

    # ---- 其余 stream 接口 --------------------------------------------------
    def flush(self):
        if not self._broken and self._stream is not None:
            try:
                self._stream.flush()
            except (OSError, ValueError):
                self._mark_broken()

    def isatty(self):
        try:
            return bool(self._stream is not None and self._stream.isatty())
        except Exception:
            return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    @property
    def encoding(self):
        enc = getattr(self._stream, "encoding", None)
        return enc or "utf-8"

    @property
    def errors(self):
        return getattr(self._stream, "errors", "replace")

    def fileno(self):
        if self._stream is None:
            raise OSError("no fileno")
        return self._stream.fileno()

    def close(self):
        # 解释器退出时会 close 标准流：包一层吞异常，避免退出期再炸一次
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass

    def __getattr__(self, name):
        # 未定义的属性委托给原始流；内部字段防止初始化期无限递归
        if name.startswith("_") and name in (
            "_stream", "_name", "_broken", "_warned",
        ):
            raise AttributeError(name)
        return getattr(self._stream, name)

    def __repr__(self):
        state = "broken" if self._broken else "ok"
        return f"<ResilientStream {self._name} ({state}) wrapping {self._stream!r}>"


_installed = False


def install() -> None:
    """把 sys.stdout/sys.stderr 换成容错代理（幂等，重复调用无副作用）。"""
    global _installed
    if _installed:
        return
    _installed = True
    sys.stdout = ResilientStream(sys.stdout, "stdout")
    sys.stderr = ResilientStream(sys.stderr, "stderr")


def reset_for_tests() -> None:
    """仅供测试：允许重复 install 并重置状态。"""
    global _installed, _fallback_logger
    _installed = False
    _fallback_logger = None

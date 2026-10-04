import os
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.db.init_db import init_db
from app.db.provider_dao import seed_default_providers
from app.exceptions.exception_handlers import register_exception_handlers
# from app.db.model_dao import init_model_table
# from app.db.provider_dao import init_provider_table
from app.utils.logger import get_logger
from app import create_app
from app.services.transcriber_config_manager import TranscriberConfigManager
from events import register_handler
from ffmpeg_helper import ensure_ffmpeg_or_raise

logger = get_logger(__name__)
load_dotenv()

# 读取 .env 中的路径
static_path = os.getenv('STATIC', '/static')
out_dir = os.getenv('OUT_DIR', './static/screenshots')

# 自动创建本地目录（static 和 static/screenshots）
static_dir = "static"
uploads_dir = "uploads"
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
if not os.path.exists(uploads_dir):
    os.makedirs(uploads_dir)

if not os.path.exists(out_dir):
    os.makedirs(out_dir)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动序列拆成 5 步、每步独立日志 + 异常时打明确的 [startup N/5 FAILED] 标记。
    # 目的：用户 docker logs 一眼能看出后端死在哪一步，避免「容器一直重启但看不出原因」。
    try:
        logger.info("[startup 1/5] register_handler() — 注册事件处理器")
        register_handler()

        logger.info("[startup 2/5] init_db() — 初始化 SQLite 数据库")
        init_db()

        logger.info("[startup 3/5] TranscriberConfigManager — 读取转写器配置")
        # 转写器不再在启动时强制初始化，而是在首次生成笔记时按需创建。
        # 如果配置了不可用的类型（如 mlx-whisper 未安装），会在使用时报错而非静默回退。
        _cfg = TranscriberConfigManager().get_config()
        logger.info(
            f"           当前转写器: type={_cfg['transcriber_type']}, "
            f"model_size={_cfg['whisper_model_size']}"
        )

        logger.info("[startup 4/5] seed_default_providers() — 初始化默认 LLM 供应商")
        seed_default_providers()

        # 把已配置的代理 export 到环境变量，让 huggingface_hub（whisper 模型下载）
        # 也能走代理——含转写时的按需下载（issue #417）。
        from app.services.proxy_config_manager import ProxyConfigManager
        _proxy = ProxyConfigManager().apply_to_env()
        if _proxy:
            logger.info(f"           已应用全局代理到环境变量: {_proxy}")

        logger.info("[startup 5/5] 启动完成，等待请求")

        # 收敛上一个进程遗留的「非终态」任务（崩溃/强杀留下的 PENDING、SUMMARIZING
        # 会永远显示成「排队中」）：启动即标记为失败并写明原因。
        try:
            from app.services.note import reap_interrupted_tasks
            reaped = reap_interrupted_tasks(time.time())
            if reaped:
                logger.warning(f"           已收敛 {reaped} 个被中断的任务（标记为失败）")
        except Exception:
            logger.exception("收敛中断任务失败（不影响启动）")

        # P1-②：reap 启动 + 定时双跑。启动只收敛「上一个进程」的遗留；长常驻
        # 进程（桌面端后端不重启）中新产生的悬挂（队列丢任务/执行线程静默死亡
        # 留下的 PENDING）靠这个每 10 分钟一轮的定时收敛。reap 自带双保险，
        # 不会误伤真在跑的任务：mtime 30 秒内写过的文件跳过 + 内存已登记跳过。
        def _reap_loop() -> None:
            import threading as _threading
            from app.services.note import reap_interrupted_tasks as _reap
            while True:
                _threading.Event().wait(600)
                try:
                    n = _reap(time.time())
                    if n:
                        logger.warning(f"定时收敛 {n} 个停滞任务（标记为失败）")
                except Exception:
                    logger.exception("定时收敛停滞任务失败（下轮继续）")

        import threading
        _reap_thread = threading.Thread(target=_reap_loop, name="reap-stale-tasks", daemon=True)
        _reap_thread.start()
        logger.info("[startup 5/5] 停滞任务定时收敛已启动（每 10 分钟一轮）")

        # 自动化调度线程：稍后再看定期检查 + 汇总通知（enabled=false 时空转，
        # 见 docs/adr/0004；Windows 计划任务入口 automation_cli.py 与其文件锁互斥）
        from app.services.automation_scheduler import AutomationScheduler
        AutomationScheduler().start()
    except Exception:
        logger.exception("[startup FAILED] 后端启动期异常，详见堆栈；容器会退出并由 restart 策略决定是否重试")
        raise

    yield

app = create_app(lifespan=lifespan)

# 允许的源：本地 web 端 + Tauri 桌面端 + 浏览器扩展（chrome/edge/firefox）
# 用 regex 是因为 chrome-extension://<id> 的 id 在每次开发版加载时不固定
# Tauri 2 不同平台 webview origin 不一样，必须全列：
#   - macOS:   tauri://localhost  （自定义协议）
#   - Windows: https://tauri.localhost  （Edge WebView2）
#   - Linux:   http://tauri.localhost   （WebKitGTK）
# 漏掉哪个都会导致桌面端 fetch 返回 200 但 browser 因为 CORS 拒绝读响应，
# 表现为前端「连不上后端」但后端日志一片 200 OK。
CORS_ORIGIN_REGEX = (
    r"^chrome-extension://[a-z]+$"
    r"|^moz-extension://.+$"
    r"|^http://(localhost|127\.0\.0\.1)(:\d+)?$"
    r"|^tauri://localhost$"
    r"|^https?://tauri\.localhost$"
)


def _cors_extra_origin_patterns() -> list[str]:
    """Viewer 跨网直连：允许用户自报的 Viewer 来源（环境变量 CORS_EXTRA_ORIGINS，逗号分隔）。

    默认全开：自用场景下 Viewer 的来源是 Tailscale / ZeroTier 分配的动态地址或
    局域网 IP，逐个登记不现实；真正的访问控制由配对 token 承担，CORS 只防浏览器误读。
    需要收紧时设 CORS_EXTRA_ORIGINS 为允许的正则（逗号分隔）。
    """
    raw = os.getenv("CORS_EXTRA_ORIGINS", "").strip()
    if not raw:
        return [r"^https?://.+$", r"^[a-z][a-z0-9+.-]*://.+$"]
    return [p.strip() for p in raw.split(",") if p.strip()]


CORS_ORIGIN_REGEX = CORS_ORIGIN_REGEX + "".join(f"|{p}" for p in _cors_extra_origin_patterns())

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
register_exception_handlers(app)
app.mount(static_path, StaticFiles(directory=static_dir), name="static")
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

# Viewer 直服：手机浏览器打开 Worker 根地址即完整前端。
# 最后挂载，只接前面路由都没命中的 GET；dist 缺失时跳过，不影响 /api。
# 注意：桌面 Tauri 包不带 dist（frontendDist=../dist 指向空），此挂载自动跳过。
try:
    from app.frontend_dist import mount_frontend_dist
    mount_frontend_dist(app)
except Exception as e:
    logger.warning(f"Viewer 直服挂载跳过: {e}")









def _resolve_port() -> int:
    """解析 BACKEND_PORT：缺省 8483；非法值（非数字/越界）记警告并回退 8483。

    之前这里是裸 int(...)：用户把 .env 的端口写错一个字母，后端连一行中文
    日志都不留就 traceback 退出，前端只看到「后端启动失败」（2026-10-04 用户
    反馈端口健壮度）。现在非法值也不崩，用默认端口起，日志里写清楚。
    """
    raw = os.getenv("BACKEND_PORT", "8483")
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"BACKEND_PORT 非法（{raw!r}），回退默认端口 8483")
        return 8483
    if not 1 <= port <= 65535:
        logger.warning(f"BACKEND_PORT 越界（{port}），回退默认端口 8483")
        return 8483
    return port


def _fail_if_port_taken(host: str, port: int) -> None:
    """启动前预检：端口已被占用时直接报错退出，不进 uvicorn。

    之前是裸 uvicorn.run bind 失败抛 OSError，PyInstaller sidecar 秒退，
    前端只能看到「后端启动失败」、日志里翻 WinError 10048（2026-10-04 用户
    反馈：不知道端口被占用时会发生什么）。现在启动日志第一行就写清楚
    谁占了端口，用户照着提示杀进程或换 BACKEND_PORT 即可。
    """
    import socket

    probe_hosts = ["127.0.0.1"]
    if host not in ("127.0.0.1", "localhost"):
        # 0.0.0.0 监听所有网卡：任一回环能连上即视为已被占用
        probe_hosts = ["127.0.0.1"]
    for h in probe_hosts:
        try:
            with socket.create_connection((h, port), timeout=1.0):
                pass
        except OSError:
            return  # 连不上 = 端口空闲，正常继续
        holder = _describe_port_holder(port)
        logger.error(
            f"端口 {port} 已被占用{holder}，后端无法启动。"
            f"请关闭占用该端口的程序，或在 .env 里把 BACKEND_PORT 改成其他空闲端口后重启。"
        )
        raise SystemExit(f"端口 {port} 已被占用，启动中止{holder}")


def _describe_port_holder(port: int) -> str:
    """尽力说出占端口的是谁（Windows 下用 netstat 找 PID + 进程名），找不到就空串。"""
    import re
    import subprocess

    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            pids = {
                m.group(1)
                for line in out.splitlines()
                if f":{port}" in line and "LISTENING" in line
                for m in [re.search(r"(\d+)\s*$", line.strip())]
                if m
            }
            names = set()
            for pid in pids:
                try:
                    t = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, timeout=5,
                    ).stdout
                    first = t.strip().splitlines()[0] if t.strip() else ""
                    name = first.strip('"').split('","')[0] if first else ""
                    names.add(f"{name}(PID {pid})" if name else f"PID {pid}")
                except Exception:
                    names.add(f"PID {pid}")
            if names:
                return f"（占用者：{'、'.join(sorted(names))}）"
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    port = _resolve_port()
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    logger.info(f"Starting server on {host}:{port}")
    _fail_if_port_taken(host, port)
    uvicorn.run(app, host=host, port=port, reload=False)
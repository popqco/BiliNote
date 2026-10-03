"""自动化检查轮调度器（docs/adr/0004）。

一轮 = 拉稍后再看 → 过滤去重 → 按自动化生成配置提交任务 → 等待本轮任务结束
→ 汇总通知（每轮一条）。

双轨互斥：
- 应用内：本模块的守护线程（应用运行时生效）；
- 应用外：automation_cli.py（Windows 计划任务拉起，二者共用 config/automation.lock
  文件锁，带过期时间防上次异常退出留下的死锁）。
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.db.video_task_dao import get_task_by_video
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.services.automation_config_manager import AutomationConfigManager
from app.services.note import NOTE_OUTPUT_DIR, TERMINAL_STATUSES, NoteGenerator, find_active_task_by_video
from app.services.notifier import send_summary
from app.services.watchlater import fetch_watchlater
from app.utils.logger import get_logger

logger = get_logger(__name__)

LOCK_FILE = Path("config/automation.lock")
STATE_FILE = Path("config/automation_state.json")
STALE_LOCK_SECONDS = 6 * 3600   # 超过 6 小时的锁视为死锁（上次进程被强杀等）
ROUND_POLL_SECONDS = 60         # 轮内任务状态轮询间隔
ROUND_MAX_WAIT_SECONDS = 6 * 3600  # 单轮最长等待


def _pid_alive(pid: int) -> bool:
    """判断进程是否还在（用于识别「持有锁的进程已经死了」）。

    只靠时间过期是不够的：锁的过期阈值是 6 小时，应用崩溃/被强杀后残留的锁会让
    「立即运行一轮」在 6 小时内静默失效（2026-10-01 实测：杀掉正在跑一轮的应用后，
    再点运行一轮，接口回「已触发」但日志里连「检查轮开始」都没有）。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # 打不开句柄：权限不足说明进程还在（只是不归我们管），否则就是没了
            return k32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _RoundLock:
    """进程间互斥的简单文件锁（O_EXCL 创建，带过期清理）。"""

    def __init__(self, path: Path = LOCK_FILE, stale_seconds: int = STALE_LOCK_SECONDS):
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def _holder(self) -> Optional[int]:
        try:
            return int(json.loads(self.path.read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:
            return None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time()}, f)
                self.acquired = True
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                holder = self._holder()
                if holder is not None and not _pid_alive(holder):
                    logger.warning(f"清理残留的自动化锁（持有进程 {holder} 已退出）")
                elif age > self.stale_seconds:
                    logger.warning(f"清理过期的自动化锁（age={int(age)}s）")
                else:
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    pass
                continue
        return False

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False


class AutomationScheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._round_lock = _RoundLock()
        self._running_round = False

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._heal_stale_state()
        self._thread = threading.Thread(target=self._loop, name="automation-scheduler", daemon=True)
        self._thread.start()
        logger.info("自动化调度线程已启动（enabled 由 config/automation.json 控制）")

    def _heal_stale_state(self) -> None:
        """新进程里不可能有「正在跑」的轮次，把上次崩溃留下的 running=true 抹掉。

        否则界面会一直显示「运行中」，用户点「立即运行一轮」也看不出为什么没反应
        （状态文件里的 running 由上一轮负责收尾，进程被杀就再没人收尾了）。
        """
        try:
            state = self._load_state()
            if state.get("running"):
                logger.warning("上次的检查轮未正常收尾（进程重启），已把状态重置为空闲")
                self._update_state(running=False, phase="空闲（上次运行被中断）")
        except Exception as e:
            logger.warning(f"重置自动化状态失败：{e}")

    def stop(self) -> None:
        self._stop.set()

    # ---------------- 主循环 ----------------

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入自动化状态失败: {e}")

    def _update_state(self, **kw) -> None:
        """合并式写状态：进度/结果落进 automation_state.json，
        前端「运行一轮」后可回读真实结果（失败原因不再只留在后台日志里）。"""
        state = self._load_state()
        state.update(kw)
        self._save_state(state)

    def _loop(self) -> None:
        # 启动补查：等系统就绪后，如果距上次检查已超过半个周期就跑一轮
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                cfg = AutomationConfigManager().get_config()
                if cfg.get("enabled"):
                    interval_s = max(5, int(cfg.get("interval_minutes") or 120)) * 60
                    last = float(self._load_state().get("last_round_ts") or 0)
                    if time.time() - last >= interval_s / 2:
                        self.run_round_once_safe(cfg)
            except Exception as e:
                logger.error(f"自动化调度线程异常: {e}", exc_info=True)
            cfg = AutomationConfigManager().get_config()
            wait_s = max(5, int(cfg.get("interval_minutes") or 120)) * 60
            self._stop.wait(wait_s)

    # ---------------- 单轮执行 ----------------

    def run_round_once_safe(self, cfg: Optional[dict] = None) -> Dict:
        """带异常兜底的入口（HTTP 路由 / CLI / 线程共用）。"""
        if self._running_round:
            # 说明白为什么没跑：不然界面只会看到「已触发」而进度纹丝不动
            self._update_state(last_error="本进程已有一轮在运行，本次触发被忽略")
            return {"skipped": "当前进程已有检查轮在运行"}
        if not self._round_lock.acquire():
            self._update_state(last_error="另一个入口（应用内调度或计划任务）正在跑一轮，本次触发被忽略")
            return {"skipped": "另一个入口的检查轮正在运行（文件锁被占用）"}
        self._running_round = True
        self._update_state(running=True, phase="拉取稍后再看")
        try:
            result = self.run_round_once(cfg)
        except Exception as e:
            logger.error(f"检查轮失败: {e}", exc_info=True)
            self._update_state(running=False, phase="失败", last_error=str(e))
            return {"error": str(e)}
        finally:
            self._running_round = False
            self._round_lock.release()
        if isinstance(result, dict) and result.get("error"):
            self._update_state(running=False, phase="失败", last_error=result["error"])
        return result

    def run_round_once(self, cfg: Optional[dict] = None) -> Dict:
        cfg = cfg or AutomationConfigManager().get_config()

        # 前置校验：生成配置不完整直接跳过（否则会提交出必然失败的任务）
        gen = cfg.get("gen") or {}
        if not gen.get("model_name") or not gen.get("provider_id"):
            msg = "自动化生成配置不完整：请到「设置 → 自动化」选择模型后再运行"
            logger.warning(msg)
            return {"error": msg}

        started = datetime.now()
        logger.info(f"=== 自动化检查轮开始 {started:%Y-%m-%d %H:%M:%S} ===")

        items = fetch_watchlater(max_items=100)
        mode = str(cfg.get("mode") or "all")
        window_days = int(cfg.get("window_days") or 7)
        max_per_round = max(1, int(cfg.get("max_per_round") or 5))
        cutoff = time.time() - window_days * 86400

        submitted: List[dict] = []
        skipped: List[dict] = []

        for it in items:
            bv = it.get("bvid")
            if not bv:
                continue
            if get_task_by_video(bv, "bilibili"):
                skipped.append({"bvid": bv, "reason": "已有成功笔记"})
                continue
            if find_active_task_by_video(bv):
                skipped.append({"bvid": bv, "reason": "已在队列/生成中"})
                continue
            if mode == "window" and it.get("add_at") and float(it["add_at"]) < cutoff:
                skipped.append({"bvid": bv, "reason": f"超出时间窗（{window_days} 天内）"})
                continue
            if len(submitted) >= max_per_round:
                skipped.append({"bvid": bv, "reason": f"超出每轮上限（{max_per_round}）"})
                continue

            task_id = self._submit_task(it, cfg)
            submitted.append({"task_id": task_id, "bvid": bv, "title": it.get("title"), "status": "PENDING"})
            logger.info(f"[自动] 已提交任务 {task_id[:8]} ← {it.get('title')} ({bv})")

        # 提交阶段先落一次进度：长轮里「已提交谁」不用等整轮结束就能看到
        self._update_state(
            phase="生成笔记",
            progress={"total_in_list": len(items), "submitted": submitted, "skipped": skipped},
        )

        result = self._wait_and_summarize(submitted, skipped, started)
        self._update_state(
            running=False,
            phase="完成",
            last_error=None,
            last_result=result,
            last_round_ts=time.time(),
            last_round_at=started.isoformat(),
        )
        logger.info(f"=== 自动化检查轮结束：提交 {len(submitted)}，跳过 {len(skipped)} ===")
        return result

    def _submit_task(self, item: dict, cfg: dict) -> str:
        gen = cfg.get("gen") or {}
        task_id = str(uuid.uuid4())
        # 先把标题/封面写进状态文件（前端生成历史无需等任务开始就能显示）
        NoteGenerator()._update_status(
            task_id,
            TaskStatus.PENDING,
            extra={
                "video_id": item["bvid"],
                "platform": "bilibili",
                "origin": "auto",
                "video_url": item["video_url"],
                "model_name": gen.get("model_name"),
                "provider_id": gen.get("provider_id"),
                "style": gen.get("style"),
                "audio_meta": {
                    "title": item.get("title"),
                    "cover_url": item.get("cover_url"),
                    "video_id": item["bvid"],
                    "duration": item.get("duration"),
                    "platform": "bilibili",
                },
            },
        )

        # 复用桌面端同一条执行路径（执行队列 / 同视频锁 / 向量索引），仅入口不同
        from app.routers.note import run_note_task

        args = (
            task_id,
            item["video_url"],
            "bilibili",
            DownloadQuality(str(gen.get("quality") or "medium")),
            False,  # link
            bool(gen.get("video_understanding")),
            gen.get("model_name"),
            gen.get("provider_id"),
            gen.get("format") or [],
            gen.get("style"),
            gen.get("extras"),
            bool(gen.get("video_understanding")),
            int(gen.get("video_interval") or 0),
            gen.get("grid_size") or [],
            item["bvid"],
        )
        threading.Thread(target=run_note_task, args=args, name=f"auto-{task_id[:8]}", daemon=True).start()
        return task_id

    def _read_status(self, task_id: str) -> Optional[dict]:
        f = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _wait_and_summarize(self, submitted: List[dict], skipped: List[dict], started: datetime) -> Dict:
        deadline = time.time() + ROUND_MAX_WAIT_SECONDS
        pending = {s["task_id"]: s for s in submitted}
        while pending and time.time() < deadline:
            if self._stop.is_set():
                break
            for tid in list(pending.keys()):
                st = self._read_status(tid)
                if st and st.get("status") in TERMINAL_STATUSES:
                    entry = pending.pop(tid)
                    entry["status"] = st.get("status")
                    entry["message"] = st.get("message")
            if pending:
                time.sleep(ROUND_POLL_SECONDS)

        result = {
            "started_at": f"{started:%m-%d %H:%M}",
            "submitted": submitted,
            "skipped": skipped,
            "still_pending": [s["task_id"] for s in pending.values()],
        }
        try:
            notify_results = send_summary(result)
            result["notify"] = notify_results
        except Exception as e:
            logger.warning(f"汇总通知发送失败: {e}")
            result["notify"] = [{"channel": "（异常）", "ok": False, "detail": str(e)}]
        return result

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
from app.services.notifier import send_round_failure_alert, send_summary
from app.services.watchlater import fetch_watchlater
from app.utils.logger import get_logger
from ffmpeg_helper import check_ffmpeg_exists

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
            # 同进程已有轮次在跑：直接忽略，不写 last_error——正在跑的那轮会自己
            # 更新进度与结果，写一条"被忽略"进去只会让界面把"跳过触发"误报成
            # "本轮失败"（2026-10-04 实测：运行中点"立即运行一轮"污染状态）。
            return {"skipped": "当前进程已有检查轮在运行"}
        if not self._round_lock.acquire():
            # 文件锁被另一入口持有：同上，不污染共享状态文件，调用方看当前进度即可。
            return {"skipped": "另一个入口的检查轮正在运行（文件锁被占用）"}
        self._running_round = True
        # 新轮启动清掉上轮的失败与进度：否则运行中界面会同时显示"本轮进度"和
        # "上轮失败原因"（2026-10-04 Cookie 误报即如此：22:32 的失败挂到 22:39
        # 的进度旁边，用户以为本轮又因 Cookie 失败）。
        self._update_state(running=True, phase="拉取稍后再看", last_error=None, progress=None)
        try:
            result = self.run_round_once(cfg)
        except Exception as e:
            logger.error(f"检查轮失败: {e}", exc_info=True)
            self._update_state(running=False, phase="失败", last_error=str(e))
            self._alert_round_failure(str(e))
            return {"error": str(e)}
        finally:
            self._running_round = False
            self._round_lock.release()
        if isinstance(result, dict) and result.get("error"):
            self._update_state(running=False, phase="失败", last_error=result["error"])
            self._alert_round_failure(result["error"])
        return result

    def _alert_round_failure(self, error: str) -> None:
        """轮级失败告警：整轮失败不提交任何任务，也就走不到 _wait_and_summarize
        的通知路径——此前自动化停摆完全静默（2026-10-10 Cookie -101 连败 1 小时+，
        零邮件）。节流双保险：
        - 基础节流：max(30, min_interval_minutes) 分钟内不重发（失败轮每 5 分钟
          必触发一次，纯靠基础节流一晚上也会刷几十封）；
        - 同文案抑制：同一错误文案 6 小时内不重发（夜间网络抖动的 SSLError
          反复触发同一句报错），文案变化（如换成 Cookie 失效）立即告警。
        """
        try:
            cfg = AutomationConfigManager().get_config()
            min_interval_min = int(((cfg.get("notify") or {}).get("min_interval_minutes")) or 0)
            basic_s = max(30, min_interval_min) * 60
            state = self._load_state()
            now_ts = time.time()
            last_ts = float(state.get("last_alert_ts") or 0)
            last_err = str(state.get("last_alert_error") or "")
            same_error = last_err == str(error)
            if last_ts > 0:
                if same_error and now_ts - last_ts < 6 * 3600:
                    return
                if not same_error and now_ts - last_ts < basic_s:
                    return
            results = send_round_failure_alert(error)
            self._update_state(last_alert_ts=now_ts, last_alert_error=str(error)[:200])
            logger.info(f"轮级失败告警已发送：{results}")
        except Exception as e:
            logger.warning(f"轮级失败告警发送失败: {e}")

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

        # ffmpeg 预检：视频理解开着但 ffmpeg 不可用时，本轮提交的每个任务都必然
        # 死在抽帧上（2026-10-05 实测：后台进程从未被健康轮询触发过 ffmpeg 探测，
        # FFMPEG_BIN_PATH 没机会前置进 PATH，约 30 轮自动化全军覆没）。
        # 这里直接判轮失败——不提交任务，也就不会发汇总邮件。
        if gen.get("video_understanding") and not check_ffmpeg_exists(use_cache=False):
            msg = "视频理解已开启，但未检测到 ffmpeg（安装后可在 .env 配置 FFMPEG_BIN_PATH 指向其 bin 目录），本轮已跳过"
            logger.error(msg)
            return {"error": msg}

        items = fetch_watchlater(max_items=100)
        mode = str(cfg.get("mode") or "all")
        window_days = int(cfg.get("window_days") or 7)
        max_per_round = max(1, int(cfg.get("max_per_round") or 5))
        cutoff = time.time() - window_days * 86400

        # 失败冷却：任务失败不落数据库，去重查不到 → 下轮立刻重试 → 持续性故障
        # （如 ffmpeg 缺失、Cookie 失效）会以「每轮一轮失败 + 一封邮件」的节奏
        # 无限重放。按 bvid 记最近一次失败，冷却窗内跳过。
        cooldown_min_cfg = cfg.get("retry_cooldown_minutes")
        cooldown_min = 30 if cooldown_min_cfg is None else max(0, int(cooldown_min_cfg))
        cooldown_s = cooldown_min * 60
        failures = self._load_state().get("failures") or {}
        now_ts = time.time()

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
            fail_rec = failures.get(bv)
            if fail_rec and cooldown_s > 0 and now_ts - float(fail_rec.get("ts") or 0) < cooldown_s:
                remain_min = int((cooldown_s - (now_ts - float(fail_rec.get("ts") or 0))) / 60) + 1
                skipped.append({
                    "bvid": bv,
                    "reason": (
                        f"上次失败冷却中（第 {fail_rec.get('count') or 1} 次："
                        f"{str(fail_rec.get('message') or '')[:60]}），约 {remain_min} 分钟后自动重试"
                    ),
                })
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

        result = self._wait_and_summarize(submitted, skipped, started, cfg)
        self._record_round_failures(result.get("submitted") or [])
        self._update_state(
            running=False,
            phase="完成",
            last_error=None,
            last_result=result,
            last_round_ts=time.time(),
            last_round_at=started.isoformat(),
            # 清掉告警记忆：本轮健康，下次新故障不应被「同文案 6 小时抑制」误吞
            last_alert_ts=None,
            last_alert_error=None,
        )
        logger.info(f"=== 自动化检查轮结束：提交 {len(submitted)}，跳过 {len(skipped)} ===")
        return result

    def _record_round_failures(self, submitted: List[dict]) -> None:
        """把本轮终态写进失败冷却表：FAILED 计数+续期，SUCCESS 清除。"""
        if not submitted:
            return
        state = self._load_state()
        failures = state.get("failures") or {}
        now_ts = time.time()
        changed = False
        for s in submitted:
            bv = s.get("bvid")
            if not bv:
                continue
            if s.get("status") == "FAILED":
                prev = failures.get(bv) or {}
                failures[bv] = {
                    "ts": now_ts,
                    "count": int(prev.get("count") or 0) + 1,
                    "message": str(s.get("message") or "")[:120],
                }
                changed = True
            elif s.get("status") == "SUCCESS" and bv in failures:
                failures.pop(bv)
                changed = True
        # 7 天前的旧记录顺手清掉，防状态文件无限膨胀
        for bv in [b for b, v in failures.items() if now_ts - float(v.get("ts") or 0) > 7 * 86400]:
            failures.pop(bv)
            changed = True
        if changed:
            self._update_state(failures=failures)

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

    def _wait_and_summarize(self, submitted: List[dict], skipped: List[dict], started: datetime,
                            cfg: Optional[dict] = None) -> Dict:
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
        # 通知节流：扫描频率（分钟级）与通知频率解耦。持续失败时扫描越勤、
        # 邮件越轰炸，但轮次结果始终完整记录在状态文件/设置页「最近一轮」里，
        # 被静默的轮次不会丢信息。
        notify_cfg = (cfg or {}).get("notify") or {}
        min_interval_min = int(notify_cfg.get("min_interval_minutes") or 0)
        if min_interval_min > 0:
            last_notify_ts = float(self._load_state().get("last_notify_ts") or 0)
            if last_notify_ts > 0 and time.time() - last_notify_ts < min_interval_min * 60:
                logger.info(
                    f"通知节流：距上次通知不足 {min_interval_min} 分钟，本轮汇总不推送（结果已记录在状态文件）"
                )
                result["notify"] = [{
                    "channel": "（节流）",
                    "ok": True,
                    "detail": f"距上次通知不足 {min_interval_min} 分钟，本轮静默",
                }]
                return result
        try:
            notify_results = send_summary(result)
            result["notify"] = notify_results
            self._update_state(last_notify_ts=time.time())
        except Exception as e:
            logger.warning(f"汇总通知发送失败: {e}")
            result["notify"] = [{"channel": "（异常）", "ok": False, "detail": str(e)}]
        return result

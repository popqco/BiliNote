"""任务状态文件与产物的唯一读写入口。

以前 `{task_id}.status.json` 有五个写入口、三套进程内登记表，读状态要跨
`services/note.py`、`routers/note.py`、`automation_scheduler.py`。
这里收拢磁盘事实源：

- ``read_status/read_result``：读状态与最终笔记；
- ``write_status/merge_status_meta``：合并式原子写，可选 run 抑制；
- ``recent/find_ids_by_video/purge/reap_interrupted``：状态文件的查询与收敛。

所有磁盘函数都接受 ``root`` 参数：默认走 ``app.paths.NOTE_OUTPUT_DIR``，
``services/note.py`` 传入自己的模块级 ``NOTE_OUTPUT_DIR``，保留现有测试对
该模块属性做 monkeypatch 的行为。

进程内的 run 身份、取消墓碑与活跃登记在 ``runs.py``；本模块不反向 import 它，
只有 reap 需要判断活跃任务时做一次惰性 import。
"""
import json
from pathlib import Path
from typing import Any, List, Optional, Union

from app.config.json_store import JsonFileStore
from app.enmus.task_status_enums import TERMINAL_STATUSES, TaskStatus
from app.paths import NOTE_OUTPUT_DIR as DEFAULT_NOTE_OUTPUT_DIR
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TASK_ARTIFACT_SUFFIXES = (
    ".status.json",
    ".json",
    "_audio.json",
    "_transcript.json",
    "_markdown.md",
    "_markdown.status.json",
    "_markdown.gpt.checkpoint.json",
)


def _resolve_root(root: Optional[Path]) -> Path:
    return Path(root) if root is not None else DEFAULT_NOTE_OUTPUT_DIR


def _status_store(task_id: str, root: Optional[Path] = None) -> JsonFileStore:
    return JsonFileStore(_resolve_root(root) / f"{task_id}.status.json")


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_status(task_id: str, root: Optional[Path] = None) -> Optional[dict]:
    if not task_id:
        return None
    return _status_store(task_id, root).read() or None


def read_result(task_id: str, root: Optional[Path] = None) -> Optional[dict]:
    if not task_id:
        return None
    return _read_json(_resolve_root(root) / f"{task_id}.json")


def _suppressed(run: Any, task_id: str, status: Union[str, TaskStatus]) -> bool:
    """旧 run（superseded）全禁写；已请求停止的 run 只允许写 CANCELLED。"""
    if run is None or getattr(run, "task_id", None) != task_id:
        return False
    if getattr(run, "superseded", False):
        logger.debug(f"陈旧 run 的状态写入已抑制 (task_id={task_id}, status={status})")
        return True
    if getattr(run, "cancelled", False) and status != TaskStatus.CANCELLED:
        logger.debug(f"任务已请求停止，抑制状态推进 (task_id={task_id}, status={status})")
        return True
    return False


def write_status(
    task_id: Optional[str],
    status: Union[str, TaskStatus],
    message: Optional[str] = None,
    extra: Optional[dict] = None,
    run: Any = None,
    root: Optional[Path] = None,
) -> None:
    """合并式写状态文件：保留 video_id/origin/audio_meta 等既有字段。"""
    if not task_id or _suppressed(run, task_id, status):
        return
    store = _status_store(task_id, root)
    data = store.read()
    data["status"] = status.value if isinstance(status, TaskStatus) else status
    if message:
        data["message"] = message
    else:
        data.pop("message", None)
    if extra:
        data.update(extra)
    store.write(data)


def merge_status_meta(
    task_id: Optional[str],
    extra: dict,
    run: Any = None,
    root: Optional[Path] = None,
) -> None:
    """只并附加字段，不改变当前 status。"""
    if not task_id:
        return
    if run is not None and getattr(run, "task_id", None) == task_id and getattr(run, "superseded", False):
        return
    store = _status_store(task_id, root)
    data = store.read()
    data.setdefault("status", TaskStatus.PENDING.value)
    data.update(extra or {})
    store.write(data)


def _result_file_meta(stem: str, root: Optional[Path]) -> dict:
    """从结果文件或音频缓存补标题/封面（老状态文件只有 status）。"""
    base = _resolve_root(root)
    result = _read_json(base / f"{stem}.json")
    if result:
        audio_meta = result.get("audio_meta")
        if isinstance(audio_meta, dict) and (audio_meta.get("title") or audio_meta.get("cover_url")):
            return audio_meta
        alt = {k: result.get(k) for k in ("title", "cover_url", "duration", "video_id", "platform") if result.get(k)}
        if alt.get("title"):
            return alt
    audio = _read_json(base / f"{stem}_audio.json")
    if audio and (audio.get("title") or audio.get("cover_url")):
        return audio
    return {}


def find_active_task_by_video(video_id: str, root: Optional[Path] = None) -> Optional[dict]:
    """查找同一 video_id 的未终态任务（提交去重用）。"""
    if not video_id:
        return None
    from app.utils.url_parser import normalize_video_id

    base = _resolve_root(root)
    video_id = normalize_video_id(video_id, "bilibili") if video_id.startswith("BV") else video_id
    try:
        candidates = sorted(
            base.glob("*.status.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in candidates:
        stem = path.name[: -len(".status.json")]
        if "_" in stem:
            continue
        data = _read_json(path)
        if not data:
            continue
        if normalize_video_id(data.get("video_id"), data.get("platform")) != video_id:
            continue
        if data.get("status") in TERMINAL_STATUSES:
            continue
        return {"task_id": stem, "status": data.get("status")}
    return None


def list_recent_tasks(limit: int = 80, root: Optional[Path] = None) -> List[dict]:
    """按最近更新倒序返回任务概要列表（供 /tasks/recent 增量同步）。"""
    base = _resolve_root(root)
    scan_cap = max(limit * 3, 240)
    items: List[dict] = []
    try:
        files = sorted(
            base.glob("*.status.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:scan_cap]
    except OSError:
        return items
    for path in files:
        stem = path.name[: -len(".status.json")]
        if "_" in stem:
            continue
        data = _read_json(path)
        if not data:
            continue
        meta = data.get("audio_meta") or {}
        if not meta.get("title"):
            fallback = _result_file_meta(stem, root)
            if fallback:
                meta = {**fallback, **{k: v for k, v in meta.items() if v}}
        try:
            updated_at = int(path.stat().st_mtime)
        except OSError:
            updated_at = 0
        items.append({
            "task_id": stem,
            "status": data.get("status"),
            "message": data.get("message"),
            "video_id": data.get("video_id") or meta.get("video_id"),
            "platform": data.get("platform") or meta.get("platform"),
            "video_url": data.get("video_url"),
            "origin": data.get("origin", "manual"),
            "model_name": data.get("model_name"),
            "provider_id": data.get("provider_id"),
            "style": data.get("style"),
            "title": meta.get("title"),
            "cover_url": meta.get("cover_url"),
            "duration": meta.get("duration"),
            "updated_at": updated_at,
            "has_result": (base / f"{stem}.json").exists(),
        })

    from app.services.recent_dedupe import pick_latest_per_video

    return pick_latest_per_video(items)[:limit]


def find_task_ids_by_video(
    video_id: str,
    platform: Optional[str] = None,
    root: Optional[Path] = None,
) -> List[str]:
    """按 video_id 找任务（老前端只传 video_id 时用）。"""
    if not video_id:
        return []
    base = _resolve_root(root)
    found: List[str] = []
    for path in base.glob("*.status.json"):
        stem = path.name[: -len(".status.json")]
        if "_" in stem:
            continue
        data = _read_json(path) or {}
        if data.get("video_id") != video_id:
            continue
        if platform and data.get("platform") and data.get("platform") != platform:
            continue
        found.append(stem)
    return found


def purge_task(task_id: str, root: Optional[Path] = None) -> dict:
    """真正删除一个任务：状态文件 + 缓存 + 导出结果 + 向量索引。"""
    if not task_id:
        return {"deleted": 0, "files": []}

    base = _resolve_root(root)
    data = read_status(task_id, root)

    deleted_files: List[str] = []
    for suffix in _TASK_ARTIFACT_SUFFIXES:
        path = base / f"{task_id}{suffix}"
        if path.exists():
            try:
                path.unlink()
                deleted_files.append(path.name)
            except OSError as e:
                logger.warning(f"删除任务文件失败 {path}: {e}")

    try:
        from app.services.vector_store import VectorStoreManager

        VectorStoreManager().delete_index(task_id)
    except Exception as e:
        logger.warning(f"删除向量索引失败 (task_id={task_id}): {e}")

    if data:
        video_id, platform = data.get("video_id"), data.get("platform")
        if video_id and not find_task_ids_by_video(video_id, platform, root):
            try:
                from app.db.video_task_dao import delete_task_by_video

                delete_task_by_video(video_id=video_id, platform=platform or "bilibili")
                logger.info(f"已清理数据库任务记录 (video_id={video_id})")
            except Exception as e:
                logger.warning(f"清理数据库任务记录失败 (video_id={video_id}): {e}")

    logger.info(f"任务已删除 (task_id={task_id})，清理文件 {len(deleted_files)} 个")
    return {"deleted": len(deleted_files), "files": deleted_files}


def reap_interrupted_tasks(process_started_at: float, root: Optional[Path] = None) -> int:
    """把上一个进程遗留的「非终态」任务标记为失败。"""
    from app.services.task_state.runs import is_task_active

    base = _resolve_root(root)
    if not base.exists():
        return 0
    reaped = 0
    cutoff = process_started_at - 30
    for path in base.glob("*.status.json"):
        stem = path.name[: -len(".status.json")]
        if "_" in stem:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        data = _read_json(path)
        if not data:
            continue
        status = data.get("status")
        if not status or status in TERMINAL_STATUSES:
            continue
        if is_task_active(stem):
            continue
        data["status"] = TaskStatus.FAILED.value
        data["message"] = "后端在任务执行中重启（或异常退出），任务已中断；请重新生成这份笔记"
        try:
            _status_store(stem, root).write(data)
            reaped += 1
            logger.warning(f"收敛中断任务：{stem}（原状态 {status}）→ FAILED")
        except OSError as e:
            logger.warning(f"写入状态文件失败 {path}: {e}")
    return reaped

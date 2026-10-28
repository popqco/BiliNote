"""笔记版本归档与读取（同一视频重新生成时保留旧笔记）。

背景（2026-10-25 用户反馈）：同一视频重新生成会复用同一个 task_id，
`save_note_to_file` 直接覆盖 `<task_id>.json`；旧正文只活在前端内存里，
重开页面/生成中切过去就看不到。这里在覆盖前把旧结果归档到
`<task_id>_versions.json`，并提供统一的版本列表（含同视频旧任务），
供阅读器在生成中/重启后切换历史版本。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services import task_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_VERSIONS = 50


def _versions_path(root: Path, task_id: str) -> Path:
    return Path(root) / f"{task_id}_versions.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return datetime.now().astimezone().isoformat(timespec="seconds")


def _content_key(content: str) -> str:
    return hashlib.sha1(content.strip().encode("utf-8")).hexdigest()


def _version_from_result(
    result: Dict[str, Any],
    task_id: str,
    created_at: str,
) -> Optional[Dict[str, Any]]:
    content = str(result.get("markdown") or "").strip()
    if not content:
        return None
    audio = result.get("audio_meta") if isinstance(result.get("audio_meta"), dict) else {}
    video_id = str(result.get("video_id") or audio.get("video_id") or "")
    try:
        stamp = int(datetime.fromisoformat(created_at).timestamp())
    except Exception:
        stamp = 0
    digest = _content_key(content)[:10]
    return {
        "ver_id": f"{task_id}-{stamp}-{digest}",
        "content": content,
        "model_name": result.get("model_name") or "",
        "provider_id": result.get("provider_id") or "",
        "style": result.get("style") or "",
        "created_at": created_at,
        "task_id": task_id,
        "video_id": video_id,
    }


def _load_archived(root: Path, task_id: str) -> List[Dict[str, Any]]:
    data = _read_json(_versions_path(root, task_id)) or {}
    versions = data.get("versions")
    if not isinstance(versions, list):
        return []
    out = []
    for item in versions:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        item = dict(item)
        item["content"] = content
        item.setdefault("ver_id", f"{task_id}-archive-{_content_key(content)[:10]}")
        item.setdefault("created_at", "")
        item.setdefault("task_id", task_id)
        out.append(item)
    return out


def _save_archived(root: Path, task_id: str, versions: List[Dict[str, Any]]) -> None:
    _write_json_atomic(
        _versions_path(root, task_id),
        {"task_id": task_id, "versions": versions[:MAX_VERSIONS]},
    )


def archive_previous_result(
    task_id: str,
    root: Path,
    new_markdown: str,
) -> bool:
    """在覆盖 `<task_id>.json` 前，把旧的成功结果归档成一个版本。

    返回 True 表示确实归档了新版本。任何异常都只记日志，绝不阻断保存。
    """
    if not task_id:
        return False
    target = Path(root) / f"{task_id}.json"
    if not target.exists():
        return False
    try:
        old = _read_json(target)
        if not old:
            return False
        old_markdown = str(old.get("markdown") or "").strip()
        if not old_markdown:
            return False
        new_text = str(new_markdown or "").strip()
        if new_text and old_markdown == new_text:
            return False
        versions = _load_archived(Path(root), task_id)
        if any(str(v.get("content") or "").strip() == old_markdown for v in versions):
            return False
        version = _version_from_result(old, task_id, _mtime_iso(target))
        if not version:
            return False
        versions.insert(0, version)
        _save_archived(Path(root), task_id, versions)
        logger.info(f"已归档笔记旧版本 (task_id={task_id}, ver_id={version['ver_id']})")
        return True
    except Exception as e:
        logger.warning(f"归档笔记旧版本失败（不阻断保存） (task_id={task_id}): {e}")
        return False


def is_archived(task_id: str, root: Path, content: str) -> bool:
    """指定正文是否已经在 `<task_id>_versions.json` 里。

    维护类写操作（截图瘦身）改的是用户数据，必须先确认旧正文真的归档成功；
    `archive_previous_result` 内部吞异常返回 False，调用方不能只看返回值。
    """
    markdown = str(content or "").strip()
    if not markdown:
        return False
    key = _content_key(markdown)
    return any(
        _content_key(str(item.get("content") or "")) == key
        for item in _load_archived(Path(root), task_id)
    )


def list_task_versions(task_id: str, root: Path) -> List[Dict[str, Any]]:
    """返回该任务的所有可读版本：归档版本 + 当前结果 + 同视频旧任务结果。

    生成中也可以调用：当前 `<task_id>.json` 仍是上一版成功结果，因此新版本
    还没跑完时，用户先看到并切换的就是旧版本。
    """
    root = Path(root)
    entries: List[Dict[str, Any]] = []

    # 1) 当前结果（生成中时就是上一版；成功后就是最新版）
    target = root / f"{task_id}.json"
    if target.exists():
        result = _read_json(target)
        if result:
            version = _version_from_result(result, task_id, _mtime_iso(target))
            if version:
                entries.append(version)

    # 2) 归档版本
    entries.extend(_load_archived(root, task_id))

    # 3) 同视频的其它历史任务（旧架构开过独立 task_id；现在已复用同卡，
    #    但存量数据里仍可能有独立任务，统一并入版本列表，避免历史丢失）
    status = task_state.read_status(task_id, root=root) or {}
    video_id = str(status.get("video_id") or "")
    platform = str(status.get("platform") or "")
    if not video_id:
        for item in entries:
            if item.get("video_id"):
                video_id = str(item["video_id"])
                break
    if video_id:
        try:
            other_ids = task_state.find_task_ids_by_video(
                video_id, platform or None, root=root
            )
        except Exception:
            other_ids = []
        for other_id in other_ids:
            if not other_id or other_id == task_id:
                continue
            other_path = root / f"{other_id}.json"
            if not other_path.exists():
                continue
            result = _read_json(other_path)
            if not result:
                continue
            version = _version_from_result(result, other_id, _mtime_iso(other_path))
            if version:
                entries.append(version)

    # 去重：正文完全相同只保留一条（时间新的优先）
    entries.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in entries:
        key = _content_key(str(item.get("content") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= MAX_VERSIONS:
            break
    return unique

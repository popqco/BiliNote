"""存量笔记维护：原片截图体检 / 瘦身（2026-10-27）。

截图密度策略只对新生成的笔记生效；用户库里已有 373 篇、3.4 万张引用
（单篇最多 790 张）。这里提供一次性的收口入口，但改的是用户数据，所以：

- 默认 `dry_run=true`，只统计不落盘；
- 真正执行前把旧正文归档进 `<task_id>_versions.json`（阅读器可切回旧版本）；
- 只改 markdown 里的图片引用，**不删除磁盘文件**（孤儿清理另做）；
- 正在生成的任务直接跳过，避免和写入流程打架。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.paths import NOTE_OUTPUT_DIR
from app.services import task_state
from app.services.note_versions import archive_previous_result
from app.services.task_config_manager import TaskConfigManager
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R
from app.utils.screenshot_policy import (
    NEAR_DUP_HAMMING,
    dhash,
    effective_limits,
    hamming,
    iter_image_refs,
    resolve_interval,
    select_image_refs,
)

router = APIRouter()
logger = get_logger(__name__)

SCREENSHOTS_DIR = Path(os.getcwd()) / "static" / "screenshots"
_CACHE_SUFFIXES = (
    "_versions.json",
    "_audio.json",
    "_transcript.json",
    ".status.json",
)
_MAX_DETAILS = 50


class SlimRequest(BaseModel):
    task_ids: Optional[list[str]] = None
    dry_run: bool = True
    interval: Optional[int] = None
    max_count: Optional[int] = None
    # 只处理引用数不低于该值的笔记（0 = 所有超上限的）
    min_images: int = 0
    # 显式点名某篇时，即使没超上限也做一次相邻去重
    include_under_limit: bool = False


def _note_files():
    for path in sorted(Path(NOTE_OUTPUT_DIR).glob("*.json")):
        if path.name.endswith(_CACHE_SUFFIXES):
            continue
        yield path


def _read_note(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _duration(data: dict) -> float:
    audio = data.get("audio_meta") if isinstance(data.get("audio_meta"), dict) else {}
    for value in (audio.get("duration"), data.get("duration")):
        try:
            number = float(value)
            if number > 0:
                return number
        except (TypeError, ValueError):
            continue
    return 0.0


def _title(data: dict) -> str:
    audio = data.get("audio_meta") if isinstance(data.get("audio_meta"), dict) else {}
    return str(audio.get("title") or data.get("title") or "")[:48]


def _limits(interval: Optional[int], max_count: Optional[int]) -> tuple[int, int]:
    cfg = TaskConfigManager()
    resolved_interval = resolve_interval(None, interval or cfg.get("screenshot_interval"))
    resolved_max = int(max_count or cfg.get("screenshot_max_count"))
    return resolved_interval, resolved_max


def _cap_for(data: dict, interval: int, max_count: int) -> int:
    cap, _, _ = effective_limits(_duration(data), interval, max_count)
    return cap


def _screenshot_file(url: str) -> Optional[Path]:
    name = url.rsplit("/", 1)[-1]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = SCREENSHOTS_DIR / name
    return path if path.is_file() else None


def _drop_refs(markdown: str, refs) -> str:
    """按位置从后往前摘掉图片引用；顺带收掉因此产生的连续空行。"""
    out = markdown
    for ref in sorted(refs, key=lambda r: r.start, reverse=True):
        out = out[: ref.start] + out[ref.end :]
    return re.sub(r"\n{3,}", "\n\n", out)


def _write_note(path: Path, data: dict, markdown: str) -> None:
    data["markdown"] = markdown
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    task_id = path.stem
    (Path(NOTE_OUTPUT_DIR) / f"{task_id}_markdown.md").write_text(markdown, encoding="utf-8")
    status_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.status.json"
    if status_path.exists():
        try:
            os.utime(status_path, None)
        except OSError:
            pass


def _disk_usage() -> dict:
    files = 0
    size = 0
    try:
        with os.scandir(SCREENSHOTS_DIR) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                    files += 1
                    size += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return {"files": files, "bytes": size}


@router.get("/maintenance/screenshot_stats")
def screenshot_stats():
    """原片截图体检：篇数/张数/超限篇数/磁盘占用（只读）。"""
    interval, max_count = _limits(None, None)
    notes = 0
    with_shots = 0
    total_images = 0
    over_limit = 0
    oversized: list[dict] = []

    for path in _note_files():
        data = _read_note(path)
        markdown = data.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            continue
        notes += 1
        refs = iter_image_refs(markdown)
        if not refs:
            continue
        with_shots += 1
        total_images += len(refs)
        cap = _cap_for(data, interval, max_count)
        if len(refs) > cap:
            over_limit += 1
            oversized.append(
                {
                    "task_id": path.stem,
                    "title": _title(data),
                    "images": len(refs),
                    "cap": cap,
                    "chars": len(markdown),
                }
            )

    oversized.sort(key=lambda item: -item["images"])
    kept_estimate = sum(min(item["images"], item["cap"]) for item in oversized)
    over_estimate = sum(item["images"] for item in oversized) - kept_estimate

    return R.success(
        {
            "notes": notes,
            "notes_with_shots": with_shots,
            "images": total_images,
            "over_limit": over_limit,
            "interval": interval,
            "max_count": max_count,
            "oversized": oversized[:10],
            "estimated_drop": over_estimate,
            "estimated_keep_after": total_images - over_estimate,
            "disk": _disk_usage(),
        }
    )


@router.post("/maintenance/screenshot_slim")
def screenshot_slim(data: SlimRequest):
    """按密度策略瘦身存量笔记；默认只演练（dry_run=true）。

    返回逐篇 before/after；执行时先归档旧正文再写回，图片文件原样保留。
    """
    interval, max_count = _limits(data.interval, data.max_count)
    targets = set(data.task_ids or [])
    details: list[dict] = []
    scanned = 0
    changed = 0
    images_before = 0
    images_after = 0
    skipped_active = 0

    for path in _note_files():
        task_id = path.stem
        if targets and task_id not in targets:
            continue
        doc = _read_note(path)
        markdown = doc.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            continue
        refs = iter_image_refs(markdown)
        if not refs:
            continue
        if data.min_images and len(refs) < int(data.min_images):
            continue
        scanned += 1
        cap = _cap_for(doc, interval, max_count)
        explicit = task_id in targets
        if len(refs) <= cap and not (explicit and data.include_under_limit):
            continue
        if task_state.is_task_active(task_id):
            skipped_active += 1
            continue

        kept = select_image_refs(markdown, refs, cap) if len(refs) > cap else list(refs)

        # 相邻同画面去重：只看已保留集合里最近 8 张，避免「同一张 PPT 贴 4 次」。
        deduped = []
        recent: list[int] = []
        for ref in kept:
            file_path = _screenshot_file(ref.url)
            digest = dhash(file_path) if file_path else None
            if digest is not None and any(
                hamming(digest, prev) <= NEAR_DUP_HAMMING for prev in recent[-8:]
            ):
                continue
            deduped.append(ref)
            if digest is not None:
                recent.append(digest)

        keep_starts = {ref.start for ref in deduped}
        dropped = [ref for ref in refs if ref.start not in keep_starts]
        if not dropped:
            continue

        new_markdown = _drop_refs(markdown, dropped)
        if not data.dry_run:
            try:
                archive_previous_result(task_id, Path(NOTE_OUTPUT_DIR), new_markdown)
                _write_note(path, doc, new_markdown)
            except Exception as exc:
                logger.warning(f"截图瘦身写回失败 (task_id={task_id}): {exc}")
                continue

        changed += 1
        images_before += len(refs)
        images_after += len(deduped)
        details.append(
            {
                "task_id": task_id,
                "title": _title(doc),
                "before": len(refs),
                "after": len(deduped),
                "dropped": len(dropped),
            }
        )

    details.sort(key=lambda item: -item["dropped"])
    logger.info(
        f"截图瘦身{'演练' if data.dry_run else '完成'}：扫描 {scanned} 篇，"
        f"影响 {changed} 篇，{images_before} → {images_after} 张；"
        f"跳过生成中 {skipped_active} 篇"
    )
    return R.success(
        {
            "dry_run": data.dry_run,
            "interval": interval,
            "max_count": max_count,
            "notes_scanned": scanned,
            "notes_changed": changed,
            "images_before": images_before,
            "images_after": images_after,
            "skipped_active": skipped_active,
            "details": details[:_MAX_DETAILS],
        }
    )

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
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.paths import NOTE_OUTPUT_DIR
from app.services import task_state
from app.services.note_versions import archive_previous_result, is_archived
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R
from app.utils.screenshot_cleanup import cleanup_residue, has_residue, remove_image_refs
from app.utils.screenshot_planner import ADAPTIVE_INTERVAL, ADAPTIVE_MAX_COUNT
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
    # 内部基准间隔：只影响没有 per-note 快照/预设的旧笔记。新笔记由自适应
    # 编排决定张数，这两项不再出现在设置界面。
    interval: Optional[int] = None
    max_count: Optional[int] = None
    # 只处理引用数不低于该值的笔记（0 = 所有超上限的）
    min_images: int = 0
    # 显式点名某篇时，即使没超上限也做一次相邻去重
    include_under_limit: bool = False
    # 顺带清理历史瘦身留下的空 bullet / 冒号残行（2026-10-28）。
    # 独立于图片数上限：已瘦身到上限以下的笔记也需要清。
    clean_residue: bool = True


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
    """维护基准：显式参数 > 自适应内部基准（90s / 60 张）。

    原片截图已无用户可调配置（2026-10-28 定版），这里只是存量笔记的
    估算口径；每篇仍优先用自己的生成快照。
    """
    try:
        resolved_interval = int(interval or ADAPTIVE_INTERVAL)
    except (TypeError, ValueError):
        resolved_interval = ADAPTIVE_INTERVAL
    try:
        resolved_max = int(max_count or ADAPTIVE_MAX_COUNT)
    except (TypeError, ValueError):
        resolved_max = ADAPTIVE_MAX_COUNT
    return max(1, resolved_interval), max(1, resolved_max)


def _note_interval(data: dict, fallback: int) -> int:
    """每篇笔记生成时的实际截图间隔。

    - 显式选了 sparse/normal/rich 的旧笔记：按该预设；
    - 其余（global/adaptive/没有 density）：用结果里的 screenshot_interval
      快照（这才是当时真正生效的值）；没有快照才退回内部基准。
    """
    density = str(data.get("screenshot_density") or "").strip().lower()
    if density in ("sparse", "normal", "rich"):
        return resolve_interval(density, fallback)
    try:
        value = int(data.get("screenshot_interval"))
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _cap_for(data: dict, interval: int, max_count: int) -> int:
    cap, _, _ = effective_limits(
        _duration(data), _note_interval(data, interval), max_count
    )
    return cap


def _screenshot_file(url: str) -> Optional[Path]:
    name = url.rsplit("/", 1)[-1]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = SCREENSHOTS_DIR / name
    return path if path.is_file() else None


def _drop_refs(markdown: str, refs) -> str:
    """摘掉图片引用，并清理因此产生的空 bullet / 冒号残行。

    2026-10-28 实测：只按区间删图会让 `- ![](url)：说明` 变成 `- ：说明`、
    `![](url)` 变成空 bullet；这些残渣在 118 篇已瘦身笔记里留下 257 条
    冒号行和 411 条空 bullet（归档的瘦身前版本里是 0）。清理规则与前端
    「隐藏截图」的 stripScreenshotImages 对齐。
    """
    return remove_image_refs(markdown, [(ref.start, ref.end) for ref in refs])


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
    residue_notes = 0
    oversized: list[dict] = []

    for path in _note_files():
        data = _read_note(path)
        markdown = data.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            continue
        notes += 1
        if has_residue(markdown):
            residue_notes += 1
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
            "notes_with_residue": residue_notes,
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
    skipped_active_ids: set[str] = set()
    skipped_archive = 0
    residue_cleaned = 0

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
            skipped_active_ids.add(task_id)
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
                # archive_previous_result 内部吞异常，只看返回值会误判；
                # 旧正文没进 versions 就绝不覆盖（否则用户失去回退能力）。
                if not is_archived(task_id, Path(NOTE_OUTPUT_DIR), markdown):
                    logger.warning(
                        f"旧正文未归档成功，跳过瘦身 (task_id={task_id})"
                    )
                    skipped_archive += 1
                    continue
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

    # 第二遍：已瘦到上限以下的旧笔记不会被上面的循环处理，但上一轮瘦身
    # 留下的 `- ：说明` / 空 bullet 还在。这里只做“残渣清理”，不动图片。
    if data.clean_residue:
        image_changed_ids = {item["task_id"] for item in details}
        for path in _note_files():
            task_id = path.stem
            if targets and task_id not in targets:
                continue
            if task_id in image_changed_ids:
                continue
            doc = _read_note(path)
            markdown = doc.get("markdown")
            if not isinstance(markdown, str) or not markdown:
                continue
            # 尊重同一个 min_images 过滤条件：调用方说「只处理 ≥N 张」时，
            # 不能顺手重写其它笔记（即使是只清残渣）。
            refs = iter_image_refs(markdown)
            if data.min_images and len(refs) < int(data.min_images):
                continue
            if not has_residue(markdown):
                continue
            if task_state.is_task_active(task_id):
                skipped_active_ids.add(task_id)
                continue
            cleaned = cleanup_residue(markdown)
            if cleaned == markdown:
                continue
            if not data.dry_run:
                try:
                    archive_previous_result(task_id, Path(NOTE_OUTPUT_DIR), cleaned)
                    if not is_archived(task_id, Path(NOTE_OUTPUT_DIR), markdown):
                        logger.warning(
                            f"旧正文未归档成功，跳过残渣清理 (task_id={task_id})"
                        )
                        skipped_archive += 1
                        continue
                    _write_note(path, doc, cleaned)
                except Exception as exc:
                    logger.warning(
                        f"残渣清理写回失败 (task_id={task_id}): {exc}"
                    )
                    continue
            residue_cleaned += 1

    details.sort(key=lambda item: -item["dropped"])
    logger.info(
        f"截图瘦身{'演练' if data.dry_run else '完成'}：扫描 {scanned} 篇，"
        f"影响 {changed} 篇，{images_before} → {images_after} 张；"
        f"清理残渣 {residue_cleaned} 篇；跳过生成中 {len(skipped_active_ids)} 篇，"
        f"归档失败 {skipped_archive} 篇"
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
            "residue_cleaned": residue_cleaned,
            "skipped_active": len(skipped_active_ids),
            "skipped_archive": skipped_archive,
            "details": details[:_MAX_DETAILS],
        }
    )

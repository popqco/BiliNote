"""删除截图标记/图片引用后的残渣清理（2026-10-28）。

背景：截图密度策略会丢弃大量 `*Screenshot-[mm:ss]*` 标记，prompt 又要求
标记下一行写「图注：…」。只删标记本体的话，正文会留下成片的无图图注；
存量瘦身按区间删图片时，也会留下 `- ：说明`、`*：说明`、空 bullet 这类
残行（2026-10-28 实测：118 篇瘦身笔记里 13 篇有冒号残行、20 篇有空 bullet）。

本模块只做**确定性的行级清理**，不碰正常正文与围栏代码块：
1. 删除标记时，如果标记独占一行（或行内只剩 `图注：…`），整行删除；
2. 顺带删除紧跟其后的「图注：…」行（允许中间隔一个空行）；
3. 删除图片引用后，清理只含 bullet + 冒号的残行、空 bullet、行首冒号，
   并压掉连续空行（代码块内原样保留）。
"""
from __future__ import annotations

import bisect
import re
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple

from app.utils.screenshot_marker import ScreenshotMarker, iter_screenshot_markers

# 图注行：允许前置列表符号 / 加粗装饰，正文以「图注/截图说明/画面说明」开头。
CAPTION_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)?\*{0,2}"
    r"(?:图注|截图说明|画面说明)\*{0,2}[ \t]*[:：].*$"
)
# 行内图注（标记与图注同一行）
_INLINE_CAPTION_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)?\*{0,2}"
    r"(?:图注|截图说明|画面说明)\*{0,2}[ \t]*[:：]"
)
# `- ：说明` / `1. ：说明`：保留 bullet，去掉冒号
_DANGLING_BULLET_COLON_RE = re.compile(
    r"(?m)^([ \t]*(?:[-+]|\d+\.[ \t]+)[ \t]*)[：:][ \t]*"
)
# `* ：说明`（星号 + 空格）是列表 bullet，保留成 `* 说明`
_STAR_BULLET_COLON_RE = re.compile(r"(?m)^([ \t]*\*[ \t]+)[：:][ \t]*")
# `*：说明`（星号紧贴冒号）是 `![](url)*：说明` 删图后的残星，整段前缀去掉
_DANGLING_STAR_COLON_RE = re.compile(r"(?m)^[ \t]*\*[：:][ \t]*")
# 只剩 bullet 的行
_EMPTY_BULLET_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]*$")
# 行首冒号（图片被删后剩下的分隔符）
_LEADING_COLON_RE = re.compile(r"(?m)^[ \t]*[：:][ \t]*")
# 围栏代码块（``` / ~~~，最多 3 个前导空格，支持 > 引用前缀）；块内不做清理
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?:>[ \t]?)*(`{3,}|~{3,})")
# 4 空格/Tab 缩进块：Markdown 缩进代码或嵌套代码，保守起见不清理
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}|\t)")


def _line_bounds(markdown: str, start: int, end: int) -> Tuple[int, int, int]:
    """返回 (行首, 行尾(不含换行), 行尾之后的位置)。"""
    line_start = markdown.rfind("\n", 0, start) + 1
    line_end = markdown.find("\n", end)
    if line_end < 0:
        return line_start, len(markdown), len(markdown)
    return line_start, line_end, line_end + 1


def _next_line_bounds(markdown: str, pos: int) -> Tuple[int, int, int]:
    start = pos
    end = markdown.find("\n", start)
    if end < 0:
        return start, len(markdown), len(markdown)
    return start, end, end + 1


def merge_spans(spans: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    ordered = sorted((s, e) for s, e in spans if e > s)
    merged: List[Tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def marker_removal_spans(
    markdown: str,
    marker: ScreenshotMarker,
    keep_starts: set[int] | None = None,
) -> List[Tuple[int, int]]:
    """删除一个标记时需要移除的字符区间（含可能的图注行）。

    标记独占一行时整行删除；标记夹在正文里时只删标记本体，避免误删句子。

    ``keep_starts`` 是本次会被保留的标记位置（`marker.start`）。同一行还有
    保留标记时，下面的共享图注必须留下，否则活下来的图会失去说明。
    """
    line_start, line_end, line_after = _line_bounds(markdown, marker.start, marker.end)
    line = markdown[line_start:line_end]
    # 该行里除当前标记外是否还有别的标记（同一行多个标记时不能整行删）
    relative = [m.start for m in iter_screenshot_markers(line)]
    other_markers = [pos for pos in relative if pos != marker.start - line_start]
    line_has_kept = bool(
        keep_starts and any(pos + line_start in keep_starts for pos in relative)
    )
    rest = line[: marker.start - line_start] + line[marker.end - line_start :]
    rest_clean = rest.strip(" \t\r\n*_~-—:：")
    spans: List[Tuple[int, int]] = []

    if not other_markers and (not rest_clean or _INLINE_CAPTION_RE.match(rest)):
        # 整行（marker 独占，或 marker 后面只跟着图注）
        spans.append((line_start, line_after))
    else:
        spans.append((marker.start, marker.end))

    # 紧跟其后的图注行：允许中间隔一个空行（模型偶尔会多写一个换行）
    search_from = line_after
    skipped_blank = 0
    while search_from < len(markdown):
        n_start, n_end, n_after = _next_line_bounds(markdown, search_from)
        line_text = markdown[n_start:n_end]
        if not line_text.strip():
            skipped_blank += 1
            if skipped_blank > 1:
                break
            search_from = n_after
            continue
        # 同一行还有保留标记时，这行图注属于那张活下来的图，不能删。
        if line_has_kept:
            break
        # 图注行里若本身还有截图标记（极少见），不能整行删，否则会连带
        # 删掉另一个标记；交给那个标记自己的处理逻辑。
        if CAPTION_LINE_RE.match(line_text) and not iter_screenshot_markers(line_text):
            spans.append((n_start, n_after))
        break
    return spans


def strip_markers_with_captions(
    markdown: str, markers: Sequence[ScreenshotMarker] | None = None
) -> str:
    """移除所有标记并顺带清掉它们的图注（无视频/降级路径用）。"""
    if not markdown:
        return markdown
    items = list(markers) if markers is not None else iter_screenshot_markers(markdown)
    if not items:
        return markdown
    spans: List[Tuple[int, int]] = []
    for marker in items:
        spans.extend(marker_removal_spans(markdown, marker))
    for start, end in sorted(merge_spans(spans), reverse=True):
        markdown = markdown[:start] + markdown[end:]
    return cleanup_residue(markdown)


def _iter_lines(markdown: str) -> Iterator[Tuple[str, bool]]:
    """逐行产出 (line, cleanable)；围栏/缩进代码块内 cleanable=False。"""
    fence: str | None = None
    for line in markdown.split("\n"):
        match = _FENCE_RE.match(line)
        if match:
            token = match.group(1)[0]
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            yield line, False
            continue
        yield line, fence is None and not _INDENTED_CODE_RE.match(line)


def _line_has_residue(line: str) -> bool:
    return bool(
        _DANGLING_BULLET_COLON_RE.match(line)
        or _STAR_BULLET_COLON_RE.match(line)
        or _DANGLING_STAR_COLON_RE.match(line)
        or _EMPTY_BULLET_LINE_RE.match(line)
        or _LEADING_COLON_RE.match(line)
    )


def has_residue(markdown: str) -> bool:
    """正文里是否有删图/删标记留下的明确残渣（不包含单纯的空白差异）。"""
    if not markdown:
        return False
    return any(cleanable and _line_has_residue(line) for line, cleanable in _iter_lines(markdown))


def cleanup_residue(markdown: str) -> str:
    """清理删图/删标记后的行级残渣（保守，只处理明确的残行）。"""
    if not markdown:
        return markdown
    out: List[str] = []
    blank_run = 0
    for line, cleanable in _iter_lines(markdown):
        if not cleanable:
            # 代码块原样保留（只统一 CRLF 的尾部 \r）
            out.append(line[:-1] if line.endswith("\r") else line)
            blank_run = 0
            continue
        original = line
        line = _DANGLING_BULLET_COLON_RE.sub(r"\1", line)
        line = _STAR_BULLET_COLON_RE.sub(r"\1", line)
        line = _DANGLING_STAR_COLON_RE.sub("", line)
        line = _LEADING_COLON_RE.sub("", line)
        if _EMPTY_BULLET_LINE_RE.match(line):
            continue
        if line != original:
            # 只有被改写的行才去尾部空白；未改写的行保留 `  ` 硬换行
            line = line.rstrip()
        if not line.strip():
            line = ""
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out.append(line)
    return "\n".join(out)


def remove_image_refs(markdown: str, spans: Sequence[Tuple[int, int]]) -> str:
    """按区间删除图片引用，并清掉受影响的空行/冒号/空 bullet/图注。

    `spans` 是图片引用在原文里的 (start, end)，调用方从 `iter_image_refs`
    或类似的解析结果里取。只删除“原本含图片、删完只剩空白/符号”的行；
    图文混排的行保留其余文字。整行删掉时顺带删除紧跟其后的「图注：…」，
    否则新格式笔记会留下无图图注（2026-10-28 复审实测）。
    """
    if not markdown or not spans:
        return markdown

    lines = markdown.split("\n")
    line_starts: List[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line) + 1

    grouped: Dict[int, List[Tuple[int, int]]] = {}
    for start, end in spans:
        idx = bisect.bisect_right(line_starts, start) - 1
        if idx < 0 or idx >= len(lines):
            continue
        rel_start = max(0, start - line_starts[idx])
        rel_end = min(len(lines[idx]), end - line_starts[idx])
        if rel_end > rel_start:
            grouped.setdefault(idx, []).append((rel_start, rel_end))

    new_lines = list(lines)
    drop: set[int] = set()
    for idx, relative_spans in grouped.items():
        line = lines[idx]
        for start, end in sorted(relative_spans, reverse=True):
            line = line[:start] + line[end:]
        # 删图后行尾常留一串空格；只对受影响行去掉，未受影响的行保留 `  ` 硬换行
        new_lines[idx] = line.rstrip()
        line = new_lines[idx]
        # 图片和图注同一行时，删图后剩下的整行就是图注，也要一起删
        if CAPTION_LINE_RE.match(line):
            drop.add(idx)
            continue
        # 只剩空 bullet（`- ` / `1. ` / `* `）或纯符号也算残行
        if not _EMPTY_BULLET_LINE_RE.match(line) and line.strip(" \t*_~-—:："):
            continue
        # 图片独占一行：整行删掉，并顺带删掉紧随的图注行（允许隔一个空行）
        drop.add(idx)
        look = idx + 1
        skipped_blank = 0
        while look < len(lines) and not lines[look].strip():
            skipped_blank += 1
            if skipped_blank > 1:
                break
            look += 1
        if (
            look < len(lines)
            and skipped_blank <= 1
            and CAPTION_LINE_RE.match(lines[look])
            and "/static/screenshots/" not in lines[look]
        ):
            drop.add(look)

    kept = [line for idx, line in enumerate(new_lines) if idx not in drop]
    return cleanup_residue("\n".join(kept))


__all__ = [
    "CAPTION_LINE_RE",
    "cleanup_residue",
    "has_residue",
    "marker_removal_spans",
    "merge_spans",
    "remove_image_refs",
    "strip_markers_with_captions",
]

"""原片截图密度策略。

背景（2026-10-27 实测）：截图数量此前完全等于模型写出的
`*Screenshot-[mm:ss]` 标记数——生成 prompt 没有密度规则，后处理也不做
校验/去重/限量。用户库里实测单篇最多 790 张、图片语法占正文 64%，既刷屏
又慢（每张一次 ffmpeg）。本模块提供确定性取舍，不依赖模型是否配合：

1. `select_markers`：生成图片之前先筛标记——同一时间戳去重、超出视频时长
   丢弃、最小间隔、全篇预算（按 H2 小节长度分配配额后等距抽稀）。
2. `dhash` / `NearDuplicateFilter`：生成之后按感知哈希丢弃相邻的同一画面
   （同一张 PPT 停留两分钟、模型对同一帧写多个标记）。

调用方见 `services/note.py::_insert_screenshots`。
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.utils.screenshot_cleanup import CAPTION_LINE_RE
from app.utils.screenshot_marker import SCREENSHOT_MARKER_PATTERN, ScreenshotMarker

# 在模块导入时抓住真 PIL：部分测试会在收集期把 sys.modules["PIL"] 换成桩
# （test_video_reader_dedupe），运行期再 import 会拿到没有 open() 的假模块。
try:  # pragma: no cover - 依赖环境
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover
    _PILImage = None

# 生成表单「截图密度」三档（秒/张）。间隔越小截图越多。
DENSITY_INTERVALS: Dict[str, int] = {
    "sparse": 180,
    "normal": 90,
    "rich": 45,
}

DEFAULT_INTERVAL = DENSITY_INTERVALS["normal"]
DEFAULT_MAX_COUNT = 60
# 配置层再夹一次，这里只是防呆（避免 0.5 秒/张这类输入把 ffmpeg 打爆）。
MAX_COUNT_CAP = 200

# 相邻画面判定：8×8 dHash，汉明距离 ≤4 视为同一画面。
NEAR_DUP_HAMMING = 4
NEAR_DUP_WINDOW_SEC = 30

_H2_RE = re.compile(r"(?m)^##[ \t]+\S")
# 只匹配单行图片引用：维护瘦身按行号定位，跨行的 alt/URL 会让行号假设失真
_IMAGE_REF_RE = re.compile(r"!\[([^\]\n]*)\]\((/static/screenshots/[^)\n]+)\)")
# 小节权重只看“真正的内容”：图片语法、截图标记、图注都是排版噪音。
# 否则图越多的小节长度越长，反而拿到越多配额（2026-10-28 实测：
# 等正文时 200 图的小节拿 28 张、2 图的小节只拿 2 张）。


@dataclass(frozen=True)
class ImageRef:
    """正文里的一个原片截图引用（存量笔记瘦身用，没有视频时间戳）。"""

    alt: str
    url: str
    start: int
    end: int


@dataclass
class PickResult:
    """筛选结果：keep 按文档顺序排列，dropped 是各原因的丢弃计数。"""

    keep: List[ScreenshotMarker] = field(default_factory=list)
    dropped: Dict[str, int] = field(default_factory=dict)

    @property
    def kept(self) -> int:
        return len(self.keep)


def resolve_interval(density: Optional[str] = None, fallback: int = DEFAULT_INTERVAL) -> int:
    """密度档位 → 秒/张；``global``/未知档位回退到 fallback。

    ``global`` 是 2026-10-28 新增的显式档位：表单默认跟随「设置→通用」的
    screenshot_interval，避免默认 ``normal`` 把全局值永远覆盖掉。
    """
    key = (density or "").strip().lower()
    if key in DENSITY_INTERVALS:
        return DENSITY_INTERVALS[key]
    try:
        value = int(fallback)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL
    return max(1, value)


def effective_limits(
    duration: Optional[float],
    interval: int,
    max_count: int,
) -> Tuple[int, int, int]:
    """返回 (全篇上限, 最小间隔秒, 生效间隔)。

    上限取「配置上限」与「时长/间隔」的较小值：短视频不会因为上限 60 就
    硬凑 60 张；长视频也不会超过配置上限。
    """
    interval = max(1, int(interval or DEFAULT_INTERVAL))
    limit = max(1, min(int(max_count or DEFAULT_MAX_COUNT), MAX_COUNT_CAP))
    if duration and duration > 0:
        cap = min(limit, max(1, math.ceil(float(duration) / interval)))
    else:
        cap = limit
    # 最小间隔跟着密度走：标准 90s → 15s；丰富 45s → 8s；精简 180s → 30s。
    min_gap = max(8, min(30, interval // 6))
    return cap, min_gap, interval


def section_spans(markdown: str) -> List[Tuple[int, int]]:
    """按 `## ` 二级标题切段，返回 [(start, end)]（含首个标题前的前言段）。"""
    starts = [m.start() for m in _H2_RE.finditer(markdown or "")]
    if not starts:
        return [(0, len(markdown or ""))]
    spans: List[Tuple[int, int]] = []
    if starts[0] > 0:
        spans.append((0, starts[0]))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(markdown)
        spans.append((start, end))
    return spans


def _visible_length(text: str) -> int:
    """小节里“非图片/非标记/非图注”的字符数，用作配额权重。"""
    cleaned = _IMAGE_REF_RE.sub("", text or "")
    cleaned = re.sub(SCREENSHOT_MARKER_PATTERN, "", cleaned)
    cleaned = CAPTION_LINE_RE.sub("", cleaned)
    # 空白不参与权重；否则模型多写空行也会影响配额
    return len(re.sub(r"\s+", "", cleaned))


def _even_pick(items: Sequence, k: int) -> List:
    """从序列里等距取 k 个（保留首尾，覆盖全片而不是只保开头）。

    k >= len 原样返回；k == 1 取中点——单张截图落在全篇正中比落在片头更有用。
    """
    n = len(items)
    if k >= n:
        return list(items)
    if k <= 0:
        return []
    if k == 1:
        return [items[n // 2]]
    idxs = [round(i * (n - 1) / (k - 1)) for i in range(k)]
    # 极端 n/k 比例下 round 可能产生重复下标，去重后可能少于 k 个。
    return [items[i] for i in dict.fromkeys(idxs)]


def _apply_section_quota(markdown: str, items: Sequence, cap: int, start_of) -> Tuple[List, int]:
    """按 H2 小节长度分配配额；超配额的小节内等距抽稀。

    返回 (保留项, 因小节配额丢弃的数量)。`start_of(item)` 给出该项在 markdown
    里的字符位置。
    """
    spans = section_spans(markdown)
    if not spans or not items:
        return list(items), 0
    span_starts = [s for s, _ in spans]
    buckets: List[List] = [[] for _ in spans]
    for item in items:
        idx = bisect.bisect_right(span_starts, start_of(item)) - 1
        buckets[max(0, idx)].append(item)

    weights = [_visible_length(markdown[start:end]) for start, end in spans]
    if not any(weights):
        weights = [max(1, end - start) for start, end in spans]
    total_len = sum(max(1, weight) for weight in weights)
    kept: List = []
    dropped = 0
    for weight, bucket in zip(weights, buckets):
        if not bucket:
            continue
        quota = max(1, round(cap * max(1, weight) / total_len))
        if len(bucket) <= quota:
            kept.extend(bucket)
        else:
            kept.extend(_even_pick(bucket, quota))
            dropped += len(bucket) - quota
    return kept, dropped


def select_markers(
    markdown: str,
    markers: Sequence[ScreenshotMarker],
    *,
    duration: Optional[float] = None,
    interval: int = DEFAULT_INTERVAL,
    max_count: int = DEFAULT_MAX_COUNT,
) -> PickResult:
    """在生成截图之前筛选标记。

    顺序：时间戳去重/越界丢弃 → 最小间隔 → 小节配额 → 全篇上限。
    返回的 keep 保持文档顺序（调用方按字符位置替换）。
    """
    cap, min_gap, _ = effective_limits(duration, interval, max_count)
    dropped: Dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    # 1. 越界与重复时间戳（保留首次出现；同一秒的重复标记没有新增信息）
    valid: List[ScreenshotMarker] = []
    seen_ts = set()
    for m in markers:
        if duration and duration > 0 and m.ts > float(duration) + 2:
            drop("out_of_range")
            continue
        if m.ts in seen_ts:
            drop("duplicate_ts")
            continue
        seen_ts.add(m.ts)
        valid.append(m)

    # 2. 最小间隔：按时间贪心保留，避免 12:20 / 12:21 这种连拍
    by_time = sorted(valid, key=lambda m: (m.ts, m.start))
    spaced: List[ScreenshotMarker] = []
    last_ts: Optional[int] = None
    for m in by_time:
        if last_ts is not None and m.ts - last_ts < min_gap:
            drop("too_close")
            continue
        spaced.append(m)
        last_ts = m.ts

    # 3. 小节配额：按各小节正文长度比例分配，长小节不会独吞全部预算
    section_kept, section_dropped = _apply_section_quota(
        markdown, spaced, cap, lambda m: m.start
    )
    if section_dropped:
        dropped["section_budget"] = dropped.get("section_budget", 0) + section_dropped

    # 4. 全篇上限：小节配额取整后可能超额，再整体等距抽稀一次
    section_kept.sort(key=lambda m: m.start)
    if len(section_kept) > cap:
        dropped["global_budget"] = dropped.get("global_budget", 0) + len(section_kept) - cap
        section_kept = _even_pick(section_kept, cap)

    return PickResult(keep=section_kept, dropped=dropped)


def iter_image_refs(markdown: str) -> List[ImageRef]:
    """提取正文里的原片截图引用（`![](/static/screenshots/...)`）。"""
    return [
        ImageRef(alt=m.group(1), url=m.group(2), start=m.start(0), end=m.end(0))
        for m in _IMAGE_REF_RE.finditer(markdown or "")
    ]


def select_image_refs(markdown: str, refs: Sequence[ImageRef], cap: int) -> List[ImageRef]:
    """存量笔记瘦身：没有逐图时间戳，只按小节配额 + 全篇上限等距抽稀。"""
    cap = max(1, int(cap))
    kept, _ = _apply_section_quota(markdown, refs, cap, lambda r: r.start)
    kept.sort(key=lambda r: r.start)
    if len(kept) > cap:
        kept = _even_pick(kept, cap)
    return kept


def dhash(path: str | Path, size: int = 8) -> Optional[int]:
    """8×8 差值哈希（64 bit）。读图失败返回 None（调用方跳过去重）。"""
    if _PILImage is None:
        return None
    try:
        with _PILImage.open(path) as im:
            gray = im.convert("L").resize((size + 1, size), _PILImage.BILINEAR)
            pixels = list(gray.getdata())
    except Exception:
        return None

    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class NearDuplicateFilter:
    """记录已保留截图的 (时间, 指纹)，判定新截图是否是同一画面。

    只在时间窗内比较：同一张 PPT 在第 2 分钟和第 40 分钟各出现一次时，
    两处都保留（位置不同，读者需要各自的上下文）。
    """

    def __init__(
        self,
        window_sec: int = NEAR_DUP_WINDOW_SEC,
        threshold: int = NEAR_DUP_HAMMING,
    ) -> None:
        self.window_sec = max(1, int(window_sec))
        self.threshold = max(0, int(threshold))
        self._kept: List[Tuple[int, int]] = []

    def is_duplicate(self, ts: int, digest: int) -> bool:
        for prev_ts, prev in self._kept:
            if abs(ts - prev_ts) <= self.window_sec and hamming(prev, digest) <= self.threshold:
                return True
        self._kept.append((int(ts), int(digest)))
        return False

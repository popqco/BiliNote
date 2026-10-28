"""自适应原片截图编排（Phase-1）。

背景（2026-10-28 用户反馈）：上一版密度策略是「固定平均间隔（默认 90s）+
最小间隔 + H2 小节配额 + 全篇上限」，截图的**时间分布**仍然近似均匀。但视频
的信息密度不是线性的：教程/幻灯片段画面变化密集，口播/过场段几乎没有值得
截的画面。固定间隔会在密集段大量丢弃有价值的标记，在稀疏段又保留低价值
画面（同类项目调研见 docs/screenshot-density-research.md）。

Phase-1 不让 LLM 数张数：模型仍然只写 `*Screenshot-[mm:ss]*` 语义标记
（「这里值得看一眼」的语义证据），本模块在这些标记之上做确定性编排：

1. 视觉特征：复用 VideoReader 已抽好的采样帧。注意 VideoReader 会对**相邻
   完全相同**的帧做 MD5 去重，所以留下的帧基本都是「画面刚变化」的位置，
   相邻保留帧的时间差天然就是上一画面的停留时长（dwell）。据此计算 dHash
   变化量、边缘密度（文字/幻灯片代理）、清晰度、亮度惩罚。
2. 文本特征：转写窗口内的语速、数字/英文/公式符号密度、2-gram 主题新颖度。
3. 打分：V（变化+新颖度）/ L（边缘+停留）/ T（文本）/ M（语义标记）/
   Q（画质）加权。标记是候选，不是结论——低分标记可被预算筛掉。
4. 预算：总张数按内部基准「时长 / 90s」换算（不再有用户可调的间隔/上限），
   再按 30s 块的信息量分配；5 分钟桶保留覆盖下限，避免安静但重要的段落被
   完全跳过。
5. 选择：分数优先 + 最小间隔 + 每分钟/每小节上限；时间戳只在 ±2s 内吸附到
   更清晰的采样帧，不做大幅位移（避免图片与正文位置语义错位）。

设计约束：
- 纯确定性（无随机、稳定排序），可 golden test；
- 无新依赖：PIL + numpy（后端已有）；缺 numpy 时退化为 PIL 近似特征；
- 单篇失败不影响生成：调用方捕获异常后回退旧版 ``select_markers``；
- 不凭空添加截图：候选只来自 LLM 标记。帧驱动的「无标记区域补图」需要可靠的
  文本插入锚点，留到 Phase-2（见调研文档）。
"""
from __future__ import annotations

import bisect
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.utils.screenshot_marker import ScreenshotMarker
from app.utils.screenshot_policy import section_spans

try:  # 模块导入期抓住真 PIL：测试可能在收集期替换 sys.modules（见 policy 同名注释）
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - 依赖环境
    _PILImage = None

try:
    import numpy as _np
except Exception:  # pragma: no cover - 依赖环境
    _np = None

FRAME_DEFAULT_INTERVAL = 6
# 2026-10-28 用户定版：截图完全由自适应编排决定，不再暴露间隔/上限/模式
# 任何开关。内部预算 = 每 10 分钟约 6 张（90s/张），单篇硬上限 60 张。
ADAPTIVE_INTERVAL = 90
ADAPTIVE_MAX_COUNT = 60
# 覆盖下限的时间桶：每桶尽量至少 1 张
BUCKET_SEC = 300
# 预算分配的密度块：块内候选分数求和作为「信息量」
BLOCK_SEC = 30
BUDGET_GAMMA = 1.2
COVERAGE_SHARE = 0.30
MIN_SCORE_BASELINE = 0.35
# 任意 60s 窗口（候选 ±30s）内最多 2 张：密集段限流，不是主预算
PER_MINUTE_CAP = 2
# 只在 ±2s 内吸附：标记时间戳是模型读拼图给出的，本身可能有 ±2s 误差
SNAP_WINDOW_SEC = 2

# 打分权重（V/L/T/M/Q）
W_VISUAL = 0.35
W_LAYOUT = 0.20
W_TEXT = 0.20
W_MARKER = 0.15
W_QUALITY = 0.10

_FRAME_TS_RE = re.compile(r"frame_(\d{1,4})_(\d{2})\.jpg$")
_ENTITY_RE = re.compile(r"\d+|[A-Za-z]{2,}|[%=\[\]\(\)→←<>]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class FrameRef:
    """一个已采样的视频帧（只记录位置，像素特征按需计算）。"""

    ts: int
    path: str


@dataclass(frozen=True)
class PlannedShot:
    """计划生成的一张截图。

    ``anchor_start/end`` 是它对应的 LLM 标记在 markdown 里的字符区间——图片
    替换/删除都以标记位置为准，而不是按时间戳猜正文位置。
    """

    ts: int
    anchor_ts: int
    anchor_start: int
    anchor_end: int
    score: float
    reasons: Tuple[str, ...] = ()


@dataclass
class PlanResult:
    shots: List[PlannedShot] = field(default_factory=list)
    dropped: Dict[str, int] = field(default_factory=dict)
    stats: Dict[str, float] = field(default_factory=dict)

    def shots_by_anchor(self) -> Dict[int, List[PlannedShot]]:
        grouped: Dict[int, List[PlannedShot]] = {}
        for shot in self.shots:
            grouped.setdefault(shot.anchor_start, []).append(shot)
        return grouped


@dataclass
class _Candidate:
    marker: ScreenshotMarker
    section: int
    block: int
    bucket: int
    frame: Optional[FrameRef] = None
    score: float = 0.0
    reasons: Tuple[str, ...] = ()


# ---------------------------------------------------------------- 帧特征


class FrameIndex:
    """按时间索引采样帧；dHash/像素特征按需计算并缓存。

    一次生成里标记通常几十个，只为标记附近的帧解码像素，避免把上千张帧
    全部打开（长视频 4800 帧预算下全量解码要几十秒）。
    """

    def __init__(self, frame_dir: Optional[str | Path], interval: int = FRAME_DEFAULT_INTERVAL):
        self.interval = max(1, int(interval or FRAME_DEFAULT_INTERVAL))
        self.frames: List[FrameRef] = []
        if frame_dir:
            try:
                for name in os.listdir(frame_dir):
                    match = _FRAME_TS_RE.search(name)
                    if not match:
                        continue
                    mm, ss = int(match.group(1)), int(match.group(2))
                    # 文件名里的 ss 可能因浮点取整出现 60，按分钟进位
                    ts = mm * 60 + ss
                    self.frames.append(FrameRef(ts=ts, path=os.path.join(str(frame_dir), name)))
            except OSError:
                self.frames = []
        # 同一时间戳只留第一个（极少数并发/残留文件）
        by_ts: Dict[int, FrameRef] = {}
        for ref in sorted(self.frames, key=lambda r: (r.ts, r.path)):
            by_ts.setdefault(ref.ts, ref)
        self.frames = [by_ts[ts] for ts in sorted(by_ts)]
        self._ts = [ref.ts for ref in self.frames]
        self._digests: Dict[str, Optional[int]] = {}
        self._pixels: Dict[str, Tuple[float, float, float]] = {}

    def __bool__(self) -> bool:
        return bool(self.frames)

    def around(self, ts: float, before: float, after: float) -> List[FrameRef]:
        lo = bisect.bisect_left(self._ts, ts - before)
        hi = bisect.bisect_right(self._ts, ts + after)
        return self.frames[lo:hi]

    def nearest(self, ts: float, window: float) -> Optional[FrameRef]:
        refs = self.around(ts, window, window)
        if not refs:
            return None
        return min(refs, key=lambda r: (abs(r.ts - ts), r.ts))

    def at_or_before(self, ts: float, max_back: float) -> Optional[FrameRef]:
        """不晚于 ts 的最近帧：标记落在静态画面中间时，用它所在画面的首帧。"""
        idx = bisect.bisect_right(self._ts, ts) - 1
        if idx < 0:
            return None
        ref = self.frames[idx]
        if ts - ref.ts > max_back:
            return None
        return ref

    def frame_for(self, ts: float) -> Optional[FrameRef]:
        """标记时间对应的采样帧：优先邻近帧，退化为「所在画面的首帧」。"""
        near = self.nearest(ts, max(6, self.interval))
        if near is not None:
            return near
        return self.at_or_before(ts, max_back=self.interval * 30)

    def _index_of(self, ref: FrameRef) -> int:
        return bisect.bisect_left(self._ts, ref.ts)

    def prev(self, ref: FrameRef) -> Optional[FrameRef]:
        idx = self._index_of(ref)
        return self.frames[idx - 1] if idx > 0 else None

    def next(self, ref: FrameRef) -> Optional[FrameRef]:
        idx = self._index_of(ref)
        return self.frames[idx + 1] if idx + 1 < len(self.frames) else None

    def digest(self, ref: FrameRef) -> Optional[int]:
        """8×8 dHash。用 JPEG draft 缩解码，比全尺寸解码快一个量级。"""
        if ref.path in self._digests:
            return self._digests[ref.path]
        value: Optional[int] = None
        if _PILImage is not None:
            try:
                with _PILImage.open(ref.path) as im:
                    try:
                        im.draft("L", (64, 64))
                    except Exception:
                        pass
                    gray = im.convert("L").resize((9, 8), _PILImage.BILINEAR)
                    pixels = list(gray.getdata())
                bits = 0
                for row in range(8):
                    base = row * 9
                    for col in range(8):
                        bits = (bits << 1) | (
                            1 if pixels[base + col] > pixels[base + col + 1] else 0
                        )
                value = bits
            except Exception:
                value = None
        self._digests[ref.path] = value
        return value

    def change(self, ref: FrameRef) -> float:
        """与上一个保留帧的画面变化量（0..1）。没有上一帧时返回 0。"""
        prev = self.prev(ref)
        if prev is None:
            return 0.0
        a, b = self.digest(prev), self.digest(ref)
        if a is None or b is None:
            return 0.0
        return _hamming(a, b) / 64.0

    def dwell(self, ref: FrameRef) -> float:
        """当前画面大约停留了多久（秒），用来识别「稳定的幻灯片/界面」。

        VideoReader 会删除与上一帧**完全相同**的帧，所以到下一个保留帧的
        间隔通常就是当前画面的停留时长；但压缩噪声会让静态画面每隔几秒
        也产生一个「不完全相同」的帧，这种情况下用 change 判断：下一帧仍在
        小幅变化内才累计停留，否则按一个采样间隔计。
        """
        nxt = self.next(ref)
        if nxt is None:
            return float(max(self.interval, 60))
        gap = max(self.interval, nxt.ts - ref.ts)
        if gap > self.interval * 2:
            return float(min(120, gap))
        if self.change(nxt) < 0.04:
            return float(min(120, gap))
        return float(self.interval)

    def novelty(self, ref: FrameRef) -> float:
        """与前后 30–180s 画面的差异（避免「刚截过同一张图」）。"""
        nearest: Optional[int] = None
        for lo, hi in ((ref.ts - 180, ref.ts - 30), (ref.ts + 30, ref.ts + 180)):
            refs = self.around((lo + hi) / 2, (hi - lo) / 2, (hi - lo) / 2)
            if len(refs) > 4:
                step = max(1, len(refs) // 4)
                refs = refs[::step][:4]
            for other in refs:
                d = self.digest(other)
                cur = self.digest(ref)
                if d is None or cur is None:
                    continue
                dist = _hamming(cur, d)
                nearest = dist if nearest is None else min(nearest, dist)
        if nearest is None:
            return 0.0
        return min(1.0, nearest / 16.0)

    def pixels(self, ref: FrameRef) -> Tuple[float, float, float]:
        """返回 (边缘密度, 清晰度, 亮度)。失败时返回中性值。"""
        if ref.path in self._pixels:
            return self._pixels[ref.path]
        edge, sharp, bright = 0.0, 0.0, 0.5
        if _PILImage is not None:
            try:
                with _PILImage.open(ref.path) as im:
                    try:
                        im.draft("L", (320, 180))
                    except Exception:
                        pass
                    small = im.convert("L").resize((160, 90), _PILImage.BILINEAR)
                    if _np is not None:
                        arr = _np.asarray(small, dtype=_np.float32)
                        dx = _np.abs(_np.diff(arr, axis=1)).mean()
                        dy = _np.abs(_np.diff(arr, axis=0)).mean()
                        edge = float((dx + dy) / (2 * 255.0))
                        big = im.convert("L").resize((320, 180), _PILImage.BILINEAR)
                        big_arr = _np.asarray(big, dtype=_np.float32)
                        lap = (
                            big_arr[:-2, 1:-1]
                            + big_arr[2:, 1:-1]
                            + big_arr[1:-1, :-2]
                            + big_arr[1:-1, 2:]
                            - 4 * big_arr[1:-1, 1:-1]
                        )
                        sharp = float(lap.var())
                        bright = float(arr.mean() / 255.0)
                    else:  # pragma: no cover - 环境无 numpy 的降级路径
                        from PIL import ImageFilter, ImageStat

                        edges = small.filter(ImageFilter.FIND_EDGES)
                        edge = float(ImageStat.Stat(edges).mean[0] / 255.0)
                        sharp = float(ImageStat.Stat(edges).stddev[0] / 255.0)
                        bright = float(ImageStat.Stat(small).mean[0] / 255.0)
            except Exception:
                pass
        result = (edge, sharp, bright)
        self._pixels[ref.path] = result
        return result


# ---------------------------------------------------------------- 特征归一化


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(round((len(sorted_values) - 1) * pct / 100.0))
    return float(sorted_values[max(0, min(len(sorted_values) - 1, idx))])


def _robust_norm(values: Sequence[float], lo: float = 10, hi: float = 90) -> List[float]:
    """按整段候选的 P10–P90 拉伸；值域退化时返回中性 0.5。"""
    if not values:
        return []
    ordered = sorted(float(v) for v in values)
    p_lo, p_hi = _percentile(ordered, lo), _percentile(ordered, hi)
    if p_hi - p_lo < 1e-9:
        return [0.5] * len(values)
    return [_clip((float(v) - p_lo) / (p_hi - p_lo)) for v in values]


def _delta_norm(values: Sequence[float]) -> List[float]:
    """按 P50–P95 拉伸，奖励「明显高于常规」的变化峰值。"""
    return _robust_norm(values, lo=50, hi=95)


def _window_text(segments: Sequence, start: float, end: float) -> str:
    parts: List[str] = []
    for seg in segments or ():
        seg_start = float(getattr(seg, "start", 0) or 0)
        seg_end = float(getattr(seg, "end", 0) or 0)
        if seg_end < start or seg_start > end:
            continue
        parts.append(str(getattr(seg, "text", "") or ""))
    return "".join(parts)


def _tokens(text: str) -> set:
    cjk = _CJK_RE.findall(text)
    bigrams = {"".join(pair) for pair in zip(cjk, cjk[1:])}
    words = {word.lower() for word in re.findall(r"[A-Za-z]{2,}", text)}
    return bigrams | words


def _entity_density(text: str) -> float:
    if not text:
        return 0.0
    return len(_ENTITY_RE.findall(text)) * 100.0 / max(1, len(text))


# ---------------------------------------------------------------- 预算与选择


def total_budget(duration: Optional[float], interval: int, max_count: int) -> int:
    """总张数 = min(上限, ceil(时长 / 平均间隔))；与旧版上限语义保持一致。"""
    limit = max(1, int(max_count or 1))
    try:
        duration = float(duration or 0)
    except (TypeError, ValueError):
        duration = 0.0
    iv = max(1, int(interval or 1))
    if duration > 0:
        return max(1, min(limit, math.ceil(duration / iv)))
    return limit


def min_gap_for(interval: int) -> int:
    """最小间隔跟着预算间隔走：90s → 15s，45s → 8s，180s → 30s。"""
    return max(8, min(30, max(1, int(interval)) // 6))


def _section_of(spans: Sequence[Tuple[int, int]], pos: int) -> int:
    starts = [start for start, _ in spans]
    return max(0, bisect.bisect_right(starts, pos) - 1)


def _smooth_masses(masses: Sequence[float], radius: int = 2) -> List[float]:
    """三角核平滑：相邻块的密度互相借力，减少「30s 边界抖动」。"""
    if not masses:
        return []
    weights = [max(1, radius + 1 - abs(offset)) for offset in range(-radius, radius + 1)]

    out: List[float] = []
    for idx in range(len(masses)):
        total = 0.0
        weight_sum = 0.0
        for offset, weight in zip(range(-radius, radius + 1), weights):
            j = idx + offset
            if 0 <= j < len(masses):
                total += masses[j] * weight
                weight_sum += weight
        out.append(total / weight_sum if weight_sum else 0.0)
    return out


def _largest_remainder(raw: Sequence[float], total: int) -> List[int]:
    if not raw:
        return []
    floors = [max(0, int(math.floor(v))) for v in raw]
    remainder = max(0, int(total) - sum(floors))
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def _blocked_reason(
    candidate: _Candidate,
    selected: Sequence[_Candidate],
    section_counts: Dict[int, int],
    section_cap: int,
    cap: int,
    min_gap: int,
) -> Optional[str]:
    if len(selected) >= cap:
        return "global_budget"
    ts = candidate.marker.ts
    for other in selected:
        if abs(other.marker.ts - ts) < min_gap:
            return "too_close"
    if sum(1 for other in selected if abs(other.marker.ts - ts) <= 30) >= PER_MINUTE_CAP:
        return "per_minute"
    if section_counts.get(candidate.section, 0) >= section_cap:
        return "section_budget"
    return None


def _score_candidates(
    markers: Sequence[ScreenshotMarker],
    frames: FrameIndex,
    segments: Sequence,
    spans: Sequence[Tuple[int, int]],
) -> List[_Candidate]:
    candidates: List[_Candidate] = []
    for marker in markers:
        section = _section_of(spans, marker.start)
        candidates.append(
            _Candidate(
                marker=marker,
                section=section,
                block=marker.ts // BLOCK_SEC,
                bucket=marker.ts // BUCKET_SEC,
                frame=frames.frame_for(marker.ts) if frames else None,
            )
        )
    if not candidates:
        return []

    raw_visual = [frames.change(c.frame) if frames and c.frame else 0.0 for c in candidates]
    raw_novelty = [frames.novelty(c.frame) if frames and c.frame else 0.0 for c in candidates]
    raw_edge = [frames.pixels(c.frame)[0] if frames and c.frame else 0.0 for c in candidates]
    raw_dwell = [frames.dwell(c.frame) if frames and c.frame else 0.0 for c in candidates]
    raw_sharp = [frames.pixels(c.frame)[1] if frames and c.frame else 0.0 for c in candidates]
    raw_bright = [frames.pixels(c.frame)[2] if frames and c.frame else 0.5 for c in candidates]

    visual_norm = _delta_norm(raw_visual)
    novelty_norm = _robust_norm(raw_novelty)
    edge_norm = _robust_norm(raw_edge)
    sharp_norm = _robust_norm(raw_sharp)

    # 文本特征：窗口 ±15s，主题新颖度对比 60s 前
    speech, keyword, entity, novelty_text = [], [], [], []
    for cand in candidates:
        ts = cand.marker.ts
        text = _window_text(segments, ts - 15, ts + 15)
        prev_text = _window_text(segments, ts - 75, ts - 45)
        speech.append(len(re.sub(r"\s+", "", text)) / 30.0)
        keyword.append(float(len(_tokens(text))))
        entity.append(_entity_density(text))
        cur_tokens, prev_tokens = _tokens(text), _tokens(prev_text)
        union = cur_tokens | prev_tokens
        novelty_text.append(
            1.0 - len(cur_tokens & prev_tokens) / len(union) if union else 0.0
        )

    median_speech = sorted(speech)[len(speech) // 2] if speech else 0.0
    speech_norm = [
        _clip(rate / median_speech / 2.0) if median_speech > 1e-9 else 0.0
        for rate in speech
    ]
    keyword_norm = _robust_norm(keyword)
    entity_norm = _robust_norm(entity)
    text_novelty_norm = _robust_norm(novelty_text)

    for idx, cand in enumerate(candidates):
        if frames and cand.frame:
            v = 0.70 * visual_norm[idx] + 0.30 * novelty_norm[idx]
            layout = 0.65 * edge_norm[idx] + 0.35 * _clip(raw_dwell[idx] / 20.0)
            bright = raw_bright[idx]
            brightness_penalty = 0.5 if (bright < 0.12 or bright > 0.88) else 1.0
            quality = 0.70 * sharp_norm[idx] + 0.30 * brightness_penalty
            reasons = ("visual",)
        else:
            # 没有帧（视频理解降级/截图复用旧缓存）时中性打分，只靠文本与预算
            v = layout = quality = 0.5
            reasons = ("no_frame",)
        t = (
            0.35 * speech_norm[idx]
            + 0.30 * keyword_norm[idx]
            + 0.20 * entity_norm[idx]
            + 0.15 * text_novelty_norm[idx]
        )
        cand.score = (
            W_VISUAL * v
            + W_LAYOUT * layout
            + W_TEXT * t
            + W_MARKER * 1.0
            + W_QUALITY * quality
        )
        cand.reasons = reasons
    return candidates


def _snap(frames: FrameIndex, ts: int) -> int:
    """在 ±2s 内选更清晰/更稳定的采样帧；没有就保持原时间戳。"""
    refs = frames.around(ts, SNAP_WINDOW_SEC, SNAP_WINDOW_SEC)
    if not refs:
        return ts

    def rank(ref: FrameRef) -> Tuple[float, float, int]:
        stable = 1.0 if frames.dwell(ref) >= frames.interval * 2 else 0.0
        return (stable, frames.pixels(ref)[1], -abs(ref.ts - ts))

    return max(refs, key=rank).ts


def plan_shots(
    markdown: str,
    markers: Sequence[ScreenshotMarker],
    *,
    frame_dir: Optional[str | Path] = None,
    frame_interval: int = FRAME_DEFAULT_INTERVAL,
    segments: Optional[Sequence] = None,
    duration: Optional[float] = None,
    interval: int = ADAPTIVE_INTERVAL,
    max_count: int = ADAPTIVE_MAX_COUNT,
) -> PlanResult:
    """在 LLM 标记之上做自适应选择，返回每张图的锚点与时间戳。

    注意：``markdown`` 只用来定位 H2 小节；调用方仍按 ``anchor_start/end``
    做字符级替换，本函数不修改正文。
    """
    dropped: Dict[str, int] = {}
    budget = total_budget(duration, interval, max_count)
    if not markers:
        return PlanResult(stats={"budget": budget})

    # 1. 越界 / 重复时间戳：与旧版语义一致，重复秒只保留第一次出现
    duration_value = 0.0
    try:
        duration_value = float(duration or 0)
    except (TypeError, ValueError):
        duration_value = 0.0
    valid: List[ScreenshotMarker] = []
    seen_ts = set()
    for marker in markers:
        if duration_value > 0 and marker.ts > duration_value + 2:
            dropped["out_of_range"] = dropped.get("out_of_range", 0) + 1
            continue
        if marker.ts in seen_ts:
            dropped["duplicate_ts"] = dropped.get("duplicate_ts", 0) + 1
            continue
        seen_ts.add(marker.ts)
        valid.append(marker)
    if not valid:
        return PlanResult(dropped=dropped, stats={"budget": budget})

    # 2. 特征与打分
    frames = FrameIndex(frame_dir, frame_interval) if frame_dir else FrameIndex(None)
    spans = section_spans(markdown or "")
    candidates = _score_candidates(valid, frames, segments or (), spans)

    cap = max(1, budget)
    min_gap = min_gap_for(interval)
    # 每小节上限 = 平均份额的 3 倍（至少 2）：允许密集小节多拿，但不许一个小节
    # 独吞全部预算。单小节笔记（模型没写 H2）则该节直接拿满 cap。
    section_cap = max(2, min(cap, math.ceil(3 * cap / max(1, len(spans)))))
    buckets = max(1, math.ceil(duration_value / BUCKET_SEC)) if duration_value > 0 else 1
    coverage_budget = min(cap, buckets, max(1, round(COVERAGE_SHARE * cap)))
    density_budget = max(0, cap - coverage_budget)

    section_counts: Dict[int, int] = {}
    selected: List[_Candidate] = []
    selected_ids: set = set()

    def take(candidate: _Candidate, reason: str) -> None:
        selected.append(candidate)
        selected_ids.add(id(candidate))
        section_counts[candidate.section] = section_counts.get(candidate.section, 0) + 1
        candidate.reasons = tuple(dict.fromkeys((*candidate.reasons, reason)))

    # 3. 覆盖下限：每个 5 分钟桶至少 1 张（有候选时）。桶比预算多时
    # 等距抽取桶，保证覆盖全片而不是只覆盖开头。
    by_bucket: Dict[int, List[_Candidate]] = {}
    for cand in candidates:
        by_bucket.setdefault(cand.bucket, []).append(cand)
    bucket_ids = sorted(by_bucket)
    if len(bucket_ids) > coverage_budget:
        if coverage_budget <= 1:
            bucket_ids = [bucket_ids[len(bucket_ids) // 2]]
        else:
            pick = [
                round(i * (len(bucket_ids) - 1) / (coverage_budget - 1))
                for i in range(coverage_budget)
            ]
            bucket_ids = [bucket_ids[i] for i in dict.fromkeys(pick)]
    for bucket in bucket_ids:
        ranked = sorted(by_bucket[bucket], key=lambda c: (-c.score, c.marker.ts))
        for cand in ranked:
            if _blocked_reason(cand, selected, section_counts, section_cap, cap, min_gap):
                continue
            take(cand, "coverage")
            break

    # 4. 按信息量分配剩余预算：30s 块 mass → 平滑 → 最大余数取整
    blocks = sorted({cand.block for cand in candidates})
    block_index = {block: idx for idx, block in enumerate(blocks)}
    block_cands: Dict[int, List[_Candidate]] = {block: [] for block in blocks}
    for cand in candidates:
        block_cands[cand.block].append(cand)
    masses: List[float] = []
    for block in blocks:
        mass = sum(max(0.0, c.score - MIN_SCORE_BASELINE) for c in block_cands[block])
        masses.append(mass + 0.05)
    smoothed = _smooth_masses(masses)
    powered = [m ** BUDGET_GAMMA for m in smoothed]
    power_sum = sum(powered)
    raw_quota = [
        density_budget * value / power_sum if power_sum > 0 else 0.0 for value in powered
    ]
    quotas = _largest_remainder(raw_quota, density_budget)
    block_taken = [0] * len(blocks)

    ranked_all = sorted(candidates, key=lambda c: (-c.score, c.marker.ts))
    for cand in ranked_all:
        if id(cand) in selected_ids:
            continue
        idx = block_index.get(cand.block)
        if idx is None or block_taken[idx] >= quotas[idx]:
            continue
        if _blocked_reason(cand, selected, section_counts, section_cap, cap, min_gap):
            continue
        take(cand, "density")
        block_taken[idx] += 1

    # 4b. 配额取整 + 每分钟限流可能留下未用预算：按分数补齐，但不放宽任何
    # 硬约束（最小间隔/每小节/每分钟）。只补「预算型丢弃」，不补候选不足。
    if len(selected) < cap:
        for cand in ranked_all:
            if id(cand) in selected_ids:
                continue
            if _blocked_reason(cand, selected, section_counts, section_cap, cap, min_gap):
                continue
            take(cand, "fill")
            if len(selected) >= cap:
                break

    # 5. 未入选原因统计（按最终选择集归类，仅用于日志）
    for cand in candidates:
        if id(cand) in selected_ids:
            continue
        reason = _blocked_reason(cand, selected, section_counts, section_cap, cap, min_gap)
        idx = block_index.get(cand.block)
        if reason is None:
            reason = "block_budget" if idx is not None and block_taken[idx] >= quotas[idx] else "global_budget"
        dropped[reason] = dropped.get(reason, 0) + 1

    # 6. 时间戳吸附（±2s），按文档顺序返回
    selected.sort(key=lambda c: (c.marker.ts, c.marker.start))
    shots: List[PlannedShot] = []
    shifted = 0
    for cand in selected:
        ts = cand.marker.ts
        if frames:
            snapped = _snap(frames, ts)
            if snapped != ts:
                shifted += 1
            ts = snapped
        shots.append(
            PlannedShot(
                ts=int(ts),
                anchor_ts=int(cand.marker.ts),
                anchor_start=int(cand.marker.start),
                anchor_end=int(cand.marker.end),
                score=round(float(cand.score), 4),
                reasons=cand.reasons,
            )
        )

    stats = {
        "budget": float(cap),
        "min_gap": float(min_gap),
        "coverage_budget": float(coverage_budget),
        "density_budget": float(density_budget),
        "section_cap": float(section_cap),
        "candidates": float(len(candidates)),
        "kept": float(len(shots)),
        "snapped": float(shifted),
        "frames": float(len(frames.frames)),
    }
    return PlanResult(shots=shots, dropped=dropped, stats=stats)

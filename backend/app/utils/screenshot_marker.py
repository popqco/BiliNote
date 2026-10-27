import re
from dataclasses import dataclass
from typing import List, Tuple

# 分钟支持 1-3 位：长视频（>99 分钟，如 101:52）之前匹配不到，
# 标记原样留在笔记里且无图（2026-10-05 截图链路稳定性排查）
# 提取与清扫共用同一模式，防止两处正则漂移。
SCREENSHOT_MARKER_PATTERN = r"\*?Screenshot-(?:\[\d{1,3}:\d{2}\]|\d{1,3}:\d{2})\*?"

# 捕获分组：2/3 为方括号形态的 mm/ss，4/5 为裸 mm:ss 形态。
# 尾随星号一并吃掉：位置替换时若留下一个孤立 `*`，正文会残留强调符号
#（旧实现靠 cleanup_marker_residue 事后清扫）。
_MARKER_RE = re.compile(
    r"(\*?Screenshot-(?:\[(\d{1,3}):(\d{2})\]|(\d{1,3}):(\d{2}))\*?)"
)


@dataclass(frozen=True)
class ScreenshotMarker:
    """一个截图标记及其在 markdown 里的字符区间。

    密度策略需要「按字符位置替换」而不是 `str.replace(marker, ..., 1)`：
    同一时间戳被模型写多次时，后者换掉的是最早出现的那个，容易和筛选结果错位。
    """

    marker: str
    ts: int
    start: int
    end: int


def iter_screenshot_markers(markdown: str) -> List[ScreenshotMarker]:
    results: List[ScreenshotMarker] = []
    for match in _MARKER_RE.finditer(markdown or ""):
        mm = match.group(2) or match.group(4)
        ss = match.group(3) or match.group(5)
        results.append(
            ScreenshotMarker(
                marker=match.group(1),
                ts=int(mm) * 60 + int(ss),
                start=match.start(1),
                end=match.end(1),
            )
        )
    return results


def extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
    """兼容旧调用方的 (marker, ts) 形态。"""
    return [(m.marker, m.ts) for m in iter_screenshot_markers(markdown)]


def strip_screenshot_markers(markdown: str) -> str:
    """清扫正文里未被替换的 Screenshot 标记（含前导/尾随星号）。

    插入环节被整体跳过（无可用本地视频，视频理解被降级）或个别插入失败时，
    标记留在正文里对用户只是噪音——2026-10-13 实锤：开着视频理解的笔记
    里出现 6 行 *Screenshot-[mm:ss] 且一张图都没有。清理后的孤立星号/
    空行由下游 _cleanup_marker_residue 统一收拾。
    """
    return re.sub(SCREENSHOT_MARKER_PATTERN, "", markdown)

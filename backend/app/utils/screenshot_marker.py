import re
from typing import List, Tuple

# 分钟支持 1-3 位：长视频（>99 分钟，如 101:52）之前匹配不到，
# 标记原样留在笔记里且无图（2026-10-05 截图链路稳定性排查）
# 提取与清扫共用同一模式，防止两处正则漂移。
SCREENSHOT_MARKER_PATTERN = r"\*?Screenshot-(?:\[\d{1,3}:\d{2}\]|\d{1,3}:\d{2})\*?"


def extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
    pattern = r"(\*?Screenshot-(?:\[(\d{1,3}):(\d{2})\]|(\d{1,3}):(\d{2})))"
    results: List[Tuple[str, int]] = []
    for match in re.finditer(pattern, markdown):
        mm = match.group(2) or match.group(4)
        ss = match.group(3) or match.group(5)
        total_seconds = int(mm) * 60 + int(ss)
        results.append((match.group(1), total_seconds))
    return results


def strip_screenshot_markers(markdown: str) -> str:
    """清扫正文里未被替换的 Screenshot 标记（含前导/尾随星号）。

    插入环节被整体跳过（无可用本地视频，视频理解被降级）或个别插入失败时，
    标记留在正文里对用户只是噪音——2026-10-13 实锤：开着视频理解的笔记
    里出现 6 行 *Screenshot-[mm:ss] 且一张图都没有。清理后的孤立星号/
    空行由下游 _cleanup_marker_residue 统一收拾。
    """
    return re.sub(SCREENSHOT_MARKER_PATTERN, "", markdown)

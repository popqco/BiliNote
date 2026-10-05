import re
from typing import List, Tuple


def extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
    # 分钟支持 1-3 位：长视频（>99 分钟，如 101:52）之前匹配不到，
    # 标记原样留在笔记里且无图（2026-10-05 截图链路稳定性排查）
    pattern = r"(\*?Screenshot-(?:\[(\d{1,3}):(\d{2})\]|(\d{1,3}):(\d{2})))"
    results: List[Tuple[str, int]] = []
    for match in re.finditer(pattern, markdown):
        mm = match.group(2) or match.group(4)
        ss = match.group(3) or match.group(5)
        total_seconds = int(mm) * 60 + int(ss)
        results.append((match.group(1), total_seconds))
    return results

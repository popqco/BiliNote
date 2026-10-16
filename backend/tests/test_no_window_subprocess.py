"""子进程免弹窗回归测试（2026-10-16）。

背景：后端以无控制台方式被拉起时，每次 spawn ffmpeg 都会新分配一个控制台，
Win11 默认终端下表现为一个个终端窗口弹出（并发抽帧=一堆窗口，用户实拍
「突然出现一堆终端窗口」）。修复：所有从后端 spawn 的进程显式带
CREATE_NO_WINDOW（app/utils/proc.py 的 no_window_kwargs）。

本测试锁定两半：
1) 工具本身在 Windows 上返回 creationflags；
2) 生产代码里每一处 subprocess.run/Popen 调用都带 **no_window_kwargs()
   （源码级扫描——漏一处就复现一次窗口风暴）。
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.proc import no_window_kwargs  # noqa: E402

SCAN_FILES = sorted(ROOT.glob("app/**/*.py")) + [ROOT / "main.py", ROOT / "ffmpeg_helper.py"]
CALL_RE = re.compile(r"subprocess\.(run|Popen|call|check_output|check_call)\(")


def _call_snippets(src: str):
    """产出每个 subprocess 调用从 '(' 到配对 ')' 的源码片段（按括号配对截取）。"""
    for m in CALL_RE.finditer(src):
        i = src.index("(", m.start())
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    yield m.start(), src[m.start(): j + 1]
                    break


class TestNoWindowHelper(unittest.TestCase):
    def test_win32_returns_creationflags(self):
        kw = no_window_kwargs()
        if sys.platform == "win32":
            self.assertIn("creationflags", kw)
            self.assertTrue(kw["creationflags"] & 0x08000000)
            self.assertEqual(no_window_kwargs(), no_window_kwargs())
        else:
            self.assertEqual(kw, {})


class TestAllSpawnSitesCarryNoWindow(unittest.TestCase):
    def test_every_subprocess_call_passes_no_window(self):
        offenders = []
        for path in SCAN_FILES:
            src = path.read_text(encoding="utf-8")
            for pos, snip in _call_snippets(src):
                # 注释/文档里提到调用形态的不算：要求片段里出现实参特征
                if "no_window_kwargs" not in snip:
                    line = src[:pos].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(
            offenders, [],
            f"这些 subprocess 调用没带 **no_window_kwargs()：{offenders}",
        )


if __name__ == "__main__":
    unittest.main()

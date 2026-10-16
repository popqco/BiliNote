"""yt-dlp 输出适配器回归测试（2026-10-16）。

yt-dlp 的 to_screen/report_error 默认写 stdout/stderr；sidecar 孤儿的
stdout 管道失效后 write_string 抛 OSError 22 直接打崩下载。给 yt-dlp 挂
`logger` 参数后输出改走 app.log。这里锁定：
1) YDL_RETRY_OPTS（bilibili/youtube 五个调用点共用）必须带 logger；
2) 适配器按级别转发，且不写任何标准流。
"""

import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.downloaders.base import YDL_RETRY_OPTS  # noqa: E402
from app.utils.ydl_log import YdlLogger, ydl_logger  # noqa: E402


class TestYdlLogger(unittest.TestCase):
    def test_level_routing(self):
        adapter = YdlLogger("test_ydl_log")
        with patch.object(adapter._logger, "debug") as d, \
                patch.object(adapter._logger, "warning") as w, \
                patch.object(adapter._logger, "error") as e:
            adapter.debug("[debug] line")
            adapter.info("info line")
            adapter.warning("[warning] w")
            adapter.error("ERROR: boom")
        self.assertEqual(d.call_count, 2)  # debug + info 都降级为 DEBUG
        w.assert_called_once()
        e.assert_called_once()

    def test_singleton(self):
        self.assertIs(ydl_logger(), ydl_logger())


class TestYdlOpts(unittest.TestCase):
    def test_retry_opts_carry_logger(self):
        lg = YDL_RETRY_OPTS.get("logger")
        self.assertIsInstance(lg, YdlLogger)
        for method in ("debug", "info", "warning", "error"):
            self.assertTrue(callable(getattr(lg, method)))


if __name__ == "__main__":
    unittest.main()

"""stdout/stderr 容错代理回归测试（2026-10-16）。

根因实拍：桌面端被强杀后 sidecar 成孤儿继续服务，但 stdout/stderr 管道读端
已随 app 消失——Windows 上此后任何 write 都抛 `OSError: [Errno 22] Invalid
argument`，print / yt-dlp write_string 全崩，B 站字幕与音视频下载整链失败
（前端「生成失败: OSError: [Errno 22] Invalid argument」）。
safe_stdio.install() 后写失败被吞掉并降级到 logs/stdio_fallback.log。
"""

import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from safe_stdio import ResilientStream, install, reset_for_tests  # noqa: E402


class _BrokenStream(io.StringIO):
    """模拟管道读端消失的 stdout：任何 write/flush 都抛 OSError 22。"""

    def write(self, s):
        raise OSError(22, "Invalid argument")

    def flush(self):
        raise OSError(22, "Invalid argument")


class _EncodingBoomStream(io.StringIO):
    """可写，但第一次写抛 UnicodeEncodeError（模拟 cp936 控制台 + emoji）。"""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def write(self, s):
        self.calls += 1
        if self.calls == 1:
            raise UnicodeEncodeError("gbk", s, 0, 1, "illegal multibyte sequence")
        return super().write(s)


class TestResilientStream(unittest.TestCase):
    def test_passthrough_when_healthy(self):
        buf = io.StringIO()
        rs = ResilientStream(buf, "stdout")
        self.assertEqual(rs.write("hello\n"), 6)
        rs.flush()
        self.assertEqual(buf.getvalue(), "hello\n")
        self.assertTrue(rs.writable())

    def test_broken_stream_does_not_raise(self):
        rs = ResilientStream(_BrokenStream(), "stdout")
        self.assertEqual(rs.write("boom\n"), 5)  # 不再抛 OSError 22
        rs.flush()  # 已 broken，静默
        self.assertEqual(rs.write("again\n"), 6)  # 后续写继续吞
        self.assertEqual(rs.write(""), 0)

    def test_none_stream_swallowed(self):
        rs = ResilientStream(None, "stderr")
        rs.write("lost\n")
        rs.flush()
        self.assertEqual(rs.isatty(), False)

    def test_unicode_error_falls_back_to_replace(self):
        s = _EncodingBoomStream()
        rs = ResilientStream(s, "stdout")
        rs.write("🎬 emoji")
        self.assertIn("emoji", s.getvalue())

    def test_attribute_delegation(self):
        buf = io.StringIO()
        rs = ResilientStream(buf, "stdout")
        rs.write("abc")
        self.assertEqual(rs.getvalue(), "abc")  # StringIO 自有方法经 __getattr__

    def test_install_idempotent_and_print_safe(self):
        old_out, old_err = sys.stdout, sys.stderr
        try:
            reset_for_tests()
            sys.stdout = _BrokenStream()
            sys.stderr = _BrokenStream()
            install()
            install()  # 幂等：二次调用不叠加包装
            print("hello")  # 不抛 OSError 22
            sys.stderr.write("warn\n")  # 不抛
            sys.stdout.flush()  # 不抛
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            reset_for_tests()


if __name__ == "__main__":
    unittest.main()

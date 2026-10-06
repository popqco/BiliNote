"""Groq 转写超限音频自适应压缩回归测试（2026-10-06）。

根因实锤：65.6 分钟视频 → 下载管线产出 30MB（已是 64k mp3）→ 旧代码
超限后仍按固定 64k 重压缩（大小不变）→ 超出 Groq 25MB 上限，上传被服务端
掐断报 `Connection error.`（确定性失败，非网络抖动），且每次重试在
%TEMP% 泄漏一份 30MB 临时文件（一次排查清出 12 份）。

修复：fit_size_audio 按时长自适应码率（单声道）压到 MAX_SIZE_BYTES 以内；
压到码率下限仍超限（约 3 小时以上）则明确报错。
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.transcriber import groq as groq_mod  # noqa: E402
from app.transcriber.groq import fit_size_audio  # noqa: E402


def _make_mp3(directory: pathlib.Path, seconds: int = 30) -> pathlib.Path:
    """生成 N 秒 64k 立体声静音 mp3（模拟下载管线的真实产物）。"""
    p = directory / f"src_{seconds}s.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=stereo",
         "-t", str(seconds), "-b:a", "64k", "-loglevel", "error", str(p)],
        check=True,
    )
    return p


class TestFitSizeAudio(unittest.TestCase):
    def test_oversize_audio_compressed_under_limit(self):
        """超限音频 → 自适应码率压缩后 ≤ 上限（65 分钟长视频的主修复路径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = _make_mp3(pathlib.Path(tmp))
            # 把上限调小到源文件必然超限的量级（避免在测试里生成大文件）
            with patch.object(groq_mod, "MAX_SIZE_BYTES", 200 * 1024), \
                 patch.object(groq_mod, "MAX_SIZE_MB", 0):
                out = fit_size_audio(str(src))
                try:
                    self.assertLessEqual(os.path.getsize(out), 200 * 1024)
                    self.assertNotEqual(pathlib.Path(out), src)
                finally:
                    os.unlink(out)

    def test_floor_bitrate_still_oversize_raises_clean(self):
        """压到码率下限仍超限（超长音频）→ 明确报错，不泄漏临时文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            before = set(pathlib.Path(tmp).iterdir())
            src = _make_mp3(pathlib.Path(tmp))
            with patch.object(groq_mod, "MAX_SIZE_BYTES", 10 * 1024), \
                 patch.object(groq_mod, "MAX_SIZE_MB", 0):
                with self.assertRaises(Exception) as ctx:
                    fit_size_audio(str(src))
                self.assertIn("上限", str(ctx.exception))
            # 无临时文件泄漏
            self.assertEqual(set(pathlib.Path(tmp).iterdir()) - before, {src})

    def test_temp_file_cleaned_on_oversize(self):
        """正常压缩路径也只在目标目录留一个产物，源目录不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            before = set(pathlib.Path(tmp).iterdir())
            src = _make_mp3(pathlib.Path(tmp))
            with patch.object(groq_mod, "MAX_SIZE_BYTES", 200 * 1024):
                out = fit_size_audio(str(src))
            os.unlink(out)
            self.assertEqual(set(pathlib.Path(tmp).iterdir()) - before, {src})


if __name__ == "__main__":
    unittest.main()

"""瞬态网络错误重试回归测试（2026-10-06）。

两个用户实拍故障（muse-spark 模型本身无问题，均为网络抖动一击致命）：
1. 下载：`ERROR: [download] Got error: 219357 bytes read, 28027551 more
   expected. Giving up after 3 retries` —— CDN 读错误，但文案不匹配
   _TRANSIENT_DL_MARKERS，外层重试直接放行不重试。
2. 转写：Groq API `Connection error.` —— _transcribe_audio 单发无重试，
   网络抖一下整个任务失败，用户得手点三次「重试」。
"""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from yt_dlp.utils import DownloadError  # noqa: E402

from app.downloaders import bilibili_downloader as bbd  # noqa: E402
from app.downloaders.bilibili_downloader import _ydl_extract_download  # noqa: E402
from app.enmus.task_status_enums import TaskStatus  # noqa: E402
import app.services.note as note_svc  # noqa: E402
from app.models.transcriber_model import TranscriptResult  # noqa: E402

READ_ERROR_MSG = (
    "ERROR: [download] Got error: 219357 bytes read, 28027551 more expected. "
    "Giving up after 3 retries"
)


class _FlakyYDL:
    """前 fail_times 次 extract_info 抛指定错误，之后成功。"""

    fail_times = 1
    message = READ_ERROR_MSG
    calls = 0

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=True):
        _FlakyYDL.calls += 1
        if _FlakyYDL.calls <= _FlakyYDL.fail_times:
            raise DownloadError(_FlakyYDL.message)
        return {"id": "vid", "title": "t"}


class TestDownloadTransientMarker(unittest.TestCase):
    def setUp(self):
        _FlakyYDL.calls = 0
        _FlakyYDL.fail_times = 1
        _FlakyYDL.message = READ_ERROR_MSG

    def run_dl(self):
        with patch.object(bbd.yt_dlp, "YoutubeDL", _FlakyYDL), \
             patch.object(bbd.time, "sleep", lambda s: None):
            return _ydl_extract_download({"quiet": True}, "https://x", "/tmp", "vid")

    def test_read_error_is_retried(self):
        """CDN 读错误（Got error ... Giving up after N retries）必须走外层重试。"""
        self.assertEqual(self.run_dl().get("id"), "vid")
        self.assertEqual(_FlakyYDL.calls, 2)

    def test_ssl_error_still_retried(self):
        """既有瞬态标记（SSL EOF）不回归。"""
        _FlakyYDL.message = "ERROR: unable to download video data: SSL: UNEXPECTED_EOF_WHILE_READING"
        self.assertEqual(self.run_dl().get("id"), "vid")
        self.assertEqual(_FlakyYDL.calls, 2)

    def test_permanent_error_not_retried(self):
        """永久性错误（视频不存在等）立即抛，不浪费重试。"""
        _FlakyYDL.message = "ERROR: [bilivideo] vid: Video unavailable"
        with self.assertRaises(DownloadError):
            self.run_dl()
        self.assertEqual(_FlakyYDL.calls, 1)


class _FlakyTranscriber:
    fail_times = 2
    calls = 0

    def transcript(self, file_path: str) -> TranscriptResult:
        _FlakyTranscriber.calls += 1
        if _FlakyTranscriber.calls <= _FlakyTranscriber.fail_times:
            raise Exception("Connection error.")
        return TranscriptResult(language="zh", full_text="ok", segments=[], raw={})


def _make_generator(flaky: _FlakyTranscriber):
    gen = note_svc.NoteGenerator.__new__(note_svc.NoteGenerator)
    gen.transcriber = flaky
    gen._update_status = lambda *a, **k: None
    gen._handle_exception = lambda *a, **k: None
    return gen


class TestTranscribeRetry(unittest.TestCase):
    def setUp(self):
        _FlakyTranscriber.calls = 0
        _FlakyTranscriber.fail_times = 2

    def run_transcribe(self, tmp: str, flaky: _FlakyTranscriber):
        gen = _make_generator(flaky)
        with patch.object(note_svc.time, "sleep", lambda s: None):
            return gen._transcribe_audio(
                audio_file="fake.mp3",
                transcript_cache_file=pathlib.Path(tmp) / "t1_transcript.json",
                status_phase=TaskStatus.TRANSCRIBING,
                task_id="t1",
            )

    def test_connection_error_retried_then_success(self):
        """转写瞬态 Connection error 自动重试，第 3 次成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_transcribe(tmp, _FlakyTranscriber())
            self.assertEqual(result.full_text, "ok")
            self.assertEqual(_FlakyTranscriber.calls, 3)

    def test_all_fail_raises_after_retries(self):
        """重试耗尽后才把异常抛给 _handle_exception 链路。"""
        _FlakyTranscriber.fail_times = 99
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                self.run_transcribe(tmp, _FlakyTranscriber())
            self.assertGreaterEqual(_FlakyTranscriber.calls, 3)

    def test_cache_hit_skips_transcriber(self):
        """缓存命中不触发转写（既有行为不回归）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / "t1_transcript.json"
            cache.write_text(json.dumps({
                "language": "zh", "full_text": "cached", "segments": [],
            }), encoding="utf-8")
            result = self.run_transcribe(tmp, _FlakyTranscriber())
            self.assertEqual(result.full_text, "cached")
            self.assertEqual(_FlakyTranscriber.calls, 0)


if __name__ == "__main__":
    unittest.main()

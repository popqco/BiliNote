"""分段转写编排器回归测试（2026-10-10）。

背景：262 分钟音频压到下限码率仍有 60.1MB——mp3 在 44.1kHz 的编码器底码是
32k，压缩路线对超长音频物理走不通 → 等长硬切 N 段逐段转写再合并（用户拍板）。

覆盖：段数规划数学、真 ffmpeg 等长切分、偏移合并、段缓存断点续跑与清理、
compress_audio 16kHz 降采样（钳位修复）、引擎上限声明属性。
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.models.transcriber_model import TranscriptResult, TranscriptSegment  # noqa: E402
from app.transcriber import groq as groq_mod  # noqa: E402
from app.transcriber import segmented as seg_mod  # noqa: E402
from app.transcriber.base import Transcriber  # noqa: E402
from app.transcriber.segmented import cleanup_part_caches

MAX_BYTES = 18 * 1024 * 1024


def _make_mp3(directory: pathlib.Path, seconds: int, rate: int = 44100, bitrate: str = "64k") -> pathlib.Path:
    """生成 N 秒静音 mp3（模拟下载管线产物的形态）。"""
    p = directory / f"src_{seconds}s_{rate}.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=mono",
         "-t", str(seconds), "-b:a", bitrate, "-loglevel", "error", str(p)],
        check=True,
    )
    return p


class _StubTranscriber:
    """按文件名可控成败的假转写器：calls 记录每次调用，fail_set 命中即抛一次。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_set: set[str] = set()

    def transcript(self, file_path: str) -> TranscriptResult:
        name = pathlib.Path(file_path).name
        if name in self.fail_set:
            raise RuntimeError(f"stub failure for {name}")  # 命中即持续失败，耗尽重试
        self.calls.append(name)
        return TranscriptResult(
            language="en",
            full_text=f"txt-{name}",
            segments=[TranscriptSegment(start=0.0, end=1.0, text=f"pc-{name}")],
        )


class TestPlanSegmentCount(unittest.TestCase):
    def test_262_minutes_needs_two(self):
        # 4.37h：底码估 31.4MB / 95%×18MB → 2 段（截图里那条 262 分钟任务的档位）
        self.assertEqual(seg_mod.plan_segment_count(15720, MAX_BYTES), 2)

    def test_10_hours_needs_five(self):
        # 36000s × 2000B/s = 72MB / 17.93MB → 4.02 → 5 段
        self.assertEqual(seg_mod.plan_segment_count(36000, MAX_BYTES), 5)

    def test_just_over_threshold_needs_two(self):
        # 刚过触发线的音频（157+ 分钟）给 2 段
        self.assertEqual(seg_mod.plan_segment_count(9500, MAX_BYTES), 2)


class TestSegmentAudio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("本机无 ffmpeg，跳过真实切分测试")

    def test_equal_split_of_30s_into_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _make_mp3(pathlib.Path(tmp), 30)
            work = pathlib.Path(tmp) / "parts"
            work.mkdir()
            parts = seg_mod.segment_audio(str(src), 3, work)
            self.assertEqual([p for p, _ in parts], [str(work / f"segment_{i}.mp3") for i in range(3)])
            self.assertEqual([s for _, s in parts], [0.0, 10.0, 20.0])
            for p, _ in parts:
                dur = seg_mod.probe_duration(p)
                self.assertAlmostEqual(dur, 10.0, delta=1.5, msg=f"{p} 时长 {dur:.2f}s")


class TestMergeResults(unittest.TestCase):
    def test_offsets_language_and_raw(self):
        r0 = TranscriptResult(language="zh", full_text=" a b ",
                              segments=[TranscriptSegment(0.0, 5.0, "a"), TranscriptSegment(5.0, 8.0, "b")])
        r1 = TranscriptResult(language="en", full_text="c",
                              segments=[TranscriptSegment(0.0, 4.0, "c")])
        merged = seg_mod.merge_results([0.0, 10.0], {0: r0, 1: r1})
        self.assertEqual([(s.start, s.end, s.text) for s in merged.segments],
                         [(0.0, 5.0, "a"), (5.0, 8.0, "b"), (10.0, 14.0, "c")])
        self.assertEqual(merged.full_text, "a b c")
        self.assertEqual(merged.language, "zh")
        self.assertIsNone(merged.raw)


class TestTranscribeSegmentedResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("本机无 ffmpeg，跳过分段续跑端到端测试")

    def test_failed_run_leaves_cache_and_retry_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src = _make_mp3(root, 3)  # 3s，max=3000 → 3 段
            cache_dir = root / "caches"
            cache_dir.mkdir()

            stub = _StubTranscriber()
            stub.fail_set.add("segment_1.mp3")
            with patch.object(seg_mod.time, "sleep", lambda *_: None):
                with self.assertRaises(RuntimeError) as ctx:
                    seg_mod.transcribe_segmented(stub, str(src), 3000,
                                                 part_cache_dir=cache_dir, task_id="t1")
                self.assertIn("2/3", str(ctx.exception))
            # 首段缓存已落盘，次段没有
            self.assertTrue((cache_dir / "t1_transcript_part0.json").exists())
            self.assertFalse((cache_dir / "t1_transcript_part1.json").exists())

            stub_ok = _StubTranscriber()
            with patch.object(seg_mod.time, "sleep", lambda *_: None):
                merged = seg_mod.transcribe_segmented(stub_ok, str(src), 3000,
                                                      part_cache_dir=cache_dir, task_id="t1")
            # 断点续跑：只有段 1、2 真正转写，段 0 命中缓存
            self.assertEqual(sorted(stub_ok.calls), ["segment_1.mp3", "segment_2.mp3"])
            self.assertEqual(len(merged.segments), 3)
            offsets = {s.text: s.start for s in merged.segments}
            self.assertAlmostEqual(offsets["pc-segment_0.mp3"], 0.0, delta=1.5)
            self.assertAlmostEqual(offsets["pc-segment_1.mp3"], 1.0, delta=1.5)
            self.assertAlmostEqual(offsets["pc-segment_2.mp3"], 2.0, delta=1.5)

            cleanup_part_caches(cache_dir, "t1")
            self.assertFalse(list(cache_dir.glob("t1_transcript_part*.json")))


class TestCompressAudioRate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("本机无 ffmpeg，跳过压缩采样率测试")

    def test_compress_pins_16khz_no_bitrate_clamp(self):
        """44.1kHz 请求 16k 被编码器钳到 32k 的回归钉：16kHz 下产物不得超过 16k 码率。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = _make_mp3(pathlib.Path(tmp), 8, rate=44100, bitrate="128k")
            out = groq_mod.compress_audio(str(src), target_bitrate="16k")
            try:
                size = os.path.getsize(out)
                # 16kbps × 8s = 16KB；32k 钳位下会约 32KB。余量放宽到 20KB。
                self.assertLess(size, 20 * 1024, f"产物 {size}B 疑似被钳到 32k（未降采样）")
                stream = groq_mod.ffmpeg.probe(out)["streams"][0]
                self.assertEqual(stream.get("sample_rate"), "16000")
                self.assertEqual(stream.get("channels"), 1)
            finally:
                os.unlink(out)


class TestProviderLimitAttr(unittest.TestCase):
    def test_base_no_limit_and_groq_declares(self):
        self.assertIsNone(Transcriber.max_audio_size_bytes)
        self.assertEqual(groq_mod.GroqTranscriber.max_audio_size_bytes, groq_mod.MAX_SIZE_BYTES)


if __name__ == "__main__":
    unittest.main()

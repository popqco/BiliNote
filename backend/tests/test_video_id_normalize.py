"""多 P 视频 id 归一化回归测试（2026-10-06）。

根因实锤：多 P 视频下载器 id 带 _p1 后缀（BV1K1HB6kEiT_p1），落库用后缀
id、自动化/提交用裸 bvid——「已有成功笔记」查库永远查不到，潘通视频
15:47-16:53 被自动化连跑 6 次（每次都成功，下一轮照样重提）。
"""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.utils.url_parser import normalize_video_id  # noqa: E402
import app.services.note as note_svc  # noqa: E402


class TestNormalizeVideoId(unittest.TestCase):
    def test_bilibili_multi_p_suffix_stripped(self):
        self.assertEqual(normalize_video_id("BV1K1HB6kEiT_p1", "bilibili"), "BV1K1HB6kEiT")

    def test_bare_id_unchanged(self):
        self.assertEqual(normalize_video_id("BV1K1HB6kEiT", "bilibili"), "BV1K1HB6kEiT")

    def test_non_bilibili_untouched(self):
        """youtube 11 位 id 理论上可能以 _p<数字> 结尾，不剥。"""
        self.assertEqual(normalize_video_id("abcdede_p1x", "youtube"), "abcdede_p1x")
        self.assertEqual(normalize_video_id("abcdefgh_p1", "youtube"), "abcdefgh_p1")

    def test_empty_untouched(self):
        self.assertIsNone(normalize_video_id(None, "bilibili"))
        self.assertEqual(normalize_video_id("", "bilibili"), "")


class TestFindActiveTaskByVideoNormalize(unittest.TestCase):
    def _write_status(self, directory: pathlib.Path, task_id: str, video_id: str,
                      status: str, platform: str = "bilibili") -> None:
        (directory / f"{task_id}.status.json").write_text(json.dumps({
            "status": status, "video_id": video_id, "platform": platform,
        }), encoding="utf-8")

    def test_active_task_with_p_suffix_is_found(self):
        """正在跑的任务身份被 _p1 元数据污染后，按裸 bvid 仍要能查到。"""
        old = note_svc.NOTE_OUTPUT_DIR
        with tempfile.TemporaryDirectory() as tmp:
            note_svc.NOTE_OUTPUT_DIR = pathlib.Path(tmp)
            try:
                self._write_status(pathlib.Path(tmp), "t1", "BV1K1HB6kEiT_p1", "TRANSCRIBING")
                found = note_svc.find_active_task_by_video("BV1K1HB6kEiT")
                self.assertIsNotNone(found)
                self.assertEqual(found["task_id"], "t1")
            finally:
                note_svc.NOTE_OUTPUT_DIR = old

    def test_terminal_task_not_reported(self):
        old = note_svc.NOTE_OUTPUT_DIR
        with tempfile.TemporaryDirectory() as tmp:
            note_svc.NOTE_OUTPUT_DIR = pathlib.Path(tmp)
            try:
                self._write_status(pathlib.Path(tmp), "t2", "BV1K1HB6kEiT_p1", "SUCCESS")
                self.assertIsNone(note_svc.find_active_task_by_video("BV1K1HB6kEiT"))
            finally:
                note_svc.NOTE_OUTPUT_DIR = old


if __name__ == "__main__":
    unittest.main()

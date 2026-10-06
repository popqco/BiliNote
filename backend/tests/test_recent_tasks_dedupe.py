"""/tasks/recent 视频级去重回归测试。

用户诉求（2026-10-06）：一个视频在生成历史里只能出现一次。
背景：10-05 失败风暴时代（f7920de 修复前"失败不落库→每轮重提"）在
后端留下同一视频几十条 FAILED 状态文件，/tasks/recent 全量返回，
前端历史全量回填后历史列表出现几十张重复卡。

规则：每个 video_id 只保留一条——
  非终态(进行中) > SUCCESS > 其他终态(FAILED)；同优先级取 updated_at 新者。
  没有 video_id 的老任务不参与合并，原样保留。
"""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app.services.note as note_svc  # noqa: E402
from app.services.recent_dedupe import pick_latest_per_video  # noqa: E402


def _write_status(directory: pathlib.Path, task_id: str, video_id: str | None,
                  status: str, mtime: float, title: str = "标题") -> None:
    data = {
        "status": status,
        "video_id": video_id,
        "platform": "bilibili",
        "video_url": f"https://www.bilibili.com/video/{video_id}" if video_id else None,
        "origin": "auto",
        "model_name": "m",
        "provider_id": "p",
        "style": "minimal",
        "audio_meta": {"title": title, "cover_url": "c", "duration": 1.0},
    }
    p = directory / f"{task_id}.status.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    os.utime(p, (mtime, mtime))


class TestPickLatestPerVideo(unittest.TestCase):
    def test_storm_collapses_to_single_success(self):
        """32 条 FAILED + 1 条 SUCCESS（最新）→ 只剩那条 SUCCESS。"""
        items = [
            {"task_id": f"f{i}", "video_id": "BV1", "status": "FAILED", "updated_at": 1000 + i}
            for i in range(32)
        ]
        items.append({"task_id": "ok", "video_id": "BV1", "status": "SUCCESS", "updated_at": 2000})
        out = pick_latest_per_video(items)
        self.assertEqual([x["task_id"] for x in out], ["ok"])

    def test_active_beats_older_success(self):
        """重新生成中的 PENDING 要盖住老的 SUCCESS。"""
        items = [
            {"task_id": "old", "video_id": "BV1", "status": "SUCCESS", "updated_at": 1000},
            {"task_id": "new", "video_id": "BV1", "status": "PENDING", "updated_at": 2000},
        ]
        self.assertEqual([x["task_id"] for x in pick_latest_per_video(items)], ["new"])

    def test_success_resurfaces_when_regen_failed(self):
        """重新生成失败后，老 SUCCESS 重新可见（FAILED 盖不住 SUCCESS）。"""
        items = [
            {"task_id": "old", "video_id": "BV1", "status": "SUCCESS", "updated_at": 1000},
            {"task_id": "new", "video_id": "BV1", "status": "FAILED", "updated_at": 2000},
        ]
        self.assertEqual([x["task_id"] for x in pick_latest_per_video(items)], ["old"])

    def test_same_rank_newer_wins(self):
        items = [
            {"task_id": "a", "video_id": "BV1", "status": "SUCCESS", "updated_at": 1000},
            {"task_id": "b", "video_id": "BV1", "status": "SUCCESS", "updated_at": 2000},
        ]
        self.assertEqual([x["task_id"] for x in pick_latest_per_video(items)], ["b"])

    def test_no_video_id_passthrough_and_sort(self):
        items = [
            {"task_id": "n1", "video_id": None, "status": "FAILED", "updated_at": 500},
            {"task_id": "v1", "video_id": "BV1", "status": "SUCCESS", "updated_at": 2000},
            {"task_id": "v2", "video_id": "BV2", "status": "FAILED", "updated_at": 1500},
        ]
        out = pick_latest_per_video(items)
        self.assertEqual([x["task_id"] for x in out], ["v1", "v2", "n1"])


class TestListRecentTasksDedupe(unittest.TestCase):
    def test_list_recent_tasks_one_per_video(self):
        """integration：状态文件扫描 → 去重 → 截断，整条链符合规则。"""
        old = note_svc.NOTE_OUTPUT_DIR
        with tempfile.TemporaryDirectory() as tmp:
            note_svc.NOTE_OUTPUT_DIR = pathlib.Path(tmp)
            try:
                d = pathlib.Path(tmp)
                t0 = time.time() - 100000
                # 失败风暴：BV_STORM 32 FAILED + 最新 SUCCESS
                for i in range(32):
                    _write_status(d, f"storm-f{i:02d}", "BV_STORM", "FAILED", t0 + i)
                _write_status(d, "storm-ok", "BV_STORM", "SUCCESS", t0 + 100, "肉鸽游戏")
                # 重新生成中：PENDING 盖住老 SUCCESS
                _write_status(d, "reg-old", "BV_REGEN", "SUCCESS", t0 + 200, "老笔记")
                _write_status(d, "reg-new", "BV_REGEN", "PENDING", t0 + 300, "老笔记")
                # 重新生成失败：老 SUCCESS 顶回来
                _write_status(d, "fr-old", "BV_FAILREG", "SUCCESS", t0 + 400, "好笔记")
                _write_status(d, "fr-new", "BV_FAILREG", "FAILED", t0 + 500, "好笔记")
                # 独立视频不受影响；无 video_id 老任务原样保留
                _write_status(d, "solo", "BV_SOLO", "SUCCESS", t0 + 600, "独苗")
                _write_status(d, "legacy", None, "FAILED", t0 + 700)

                out = note_svc.list_recent_tasks(limit=80)
                by_video = {}
                for it in out:
                    by_video.setdefault(it["video_id"], []).append(it["task_id"])
                dup = {v: ids for v, ids in by_video.items() if v and len(ids) > 1}
                self.assertEqual(dup, {}, f"同一视频出现多条: {dup}")
                ids = [it["task_id"] for it in out]
                self.assertIn("storm-ok", ids)
                self.assertNotIn("storm-f00", ids)
                self.assertIn("reg-new", ids)
                self.assertNotIn("reg-old", ids)
                self.assertIn("fr-old", ids)
                self.assertNotIn("fr-new", ids)
                self.assertIn("solo", ids)
                self.assertIn("legacy", ids)
                # 排序仍是 updated_at 倒序
                times = [it["updated_at"] for it in out]
                self.assertEqual(times, sorted(times, reverse=True))
            finally:
                note_svc.NOTE_OUTPUT_DIR = old


if __name__ == "__main__":
    unittest.main()

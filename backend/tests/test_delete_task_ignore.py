"""删除笔记 → 写入自动化忽略名单（删除墓碑）+ 清理分类赋值。

不做真实 purge（会碰向量库），只打桩 purge_task；状态文件用 tmp 目录。
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.routers import note as note_router  # noqa: E402
from app.routers.note import RecordRequest, delete_task  # noqa: E402
from app.services import automation_ignore as ai  # noqa: E402
from app.services import note_categories as nc  # noqa: E402


def _write_status(directory: pathlib.Path, task_id: str, video_id: str = "BV1abc") -> None:
    data = {
        "status": "SUCCESS",
        "video_id": video_id,
        "platform": "bilibili",
        "video_url": f"https://www.bilibili.com/video/{video_id}",
        "origin": "auto",
        "model_name": "m",
        "provider_id": "p",
        "style": "minimal",
        "audio_meta": {"title": "测试视频", "cover_url": "c", "duration": 60},
    }
    (directory / f"{task_id}.status.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", notes)
    monkeypatch.setattr(ai, "IGNORE_FILE", tmp_path / "automation_ignore.json")
    monkeypatch.setattr(nc, "CATEGORY_FILE", tmp_path / "note_categories.json")
    monkeypatch.setattr(note_router.task_state, "is_task_active", lambda tid: False)

    def fake_purge(task_id, root=None):
        path = notes / f"{task_id}.status.json"
        deleted = 1 if path.exists() else 0
        path.unlink(missing_ok=True)
        return {"deleted": deleted}

    # 只替换 purge 本身：真实实现会删向量索引（测试环境不可用）
    monkeypatch.setattr(note_router.task_state, "purge_task", fake_purge)
    return notes


def test_delete_writes_tombstone_and_clears_category(env):
    _write_status(env, "tid1", "BV1abc")
    cat = nc.note_category_store().create("学习")
    nc.note_category_store().assign(["tid1"], cat["id"])

    payload = json.loads(delete_task(RecordRequest(task_id="tid1")).body)
    assert payload["code"] == 0
    assert payload["data"]["ignored"] == [{"video_id": "BV1abc", "platform": "bilibili"}]

    store = ai.automation_ignore_store()
    assert store.contains("BV1abc", "bilibili")
    assert store.items()[0]["title"] == "测试视频"
    # 分类赋值随任务一起清掉，不留孤儿映射
    assert nc.note_category_store().assignments() == {}


def test_delete_falls_back_to_request_video_id(env):
    """状态文件缺失（只传 task_id）时用请求里的 video_id 兜底。"""
    payload = json.loads(
        delete_task(RecordRequest(task_id="ghost", video_id="BV9zzz", platform="bilibili")).body
    )
    assert payload["code"] == 0
    assert payload["data"]["ignored"] == [{"video_id": "BV9zzz", "platform": "bilibili"}]
    assert ai.automation_ignore_store().contains("BV9zzz", "bilibili")


def test_delete_multi_p_normalizes_tombstone(env):
    _write_status(env, "tid2", "BV1multi_p1")
    delete_task(RecordRequest(task_id="tid2"))
    # 自动化按裸 bvid 查，_pN 后缀必须归一化掉
    assert ai.automation_ignore_store().contains("BV1multi", "bilibili")

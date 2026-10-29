"""/tasks/recent 的分类装饰与 version 指纹。

分类是独立 JSON 元数据（config/note_categories.json）：列表接口每行补
category/category_name，分类变更必须让增量轮询的 version 变化（否则手机
Viewer 永远看不到桌面端的归类）。
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.routers import note as note_router  # noqa: E402
from app.services import note_categories as nc  # noqa: E402


def _write_status(directory: pathlib.Path, task_id: str, title: str = "标题") -> None:
    data = {
        "status": "SUCCESS",
        "video_id": "BV1test",
        "platform": "bilibili",
        "video_url": "https://www.bilibili.com/video/BV1test",
        "origin": "auto",
        "model_name": "m",
        "provider_id": "p",
        "style": "minimal",
        "audio_meta": {"title": title, "cover_url": "c", "duration": 1.0},
    }
    (directory / f"{task_id}.status.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _data(resp) -> dict:
    return json.loads(resp.body)["data"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    _write_status(notes, "t1")
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", notes)
    monkeypatch.setattr(nc, "CATEGORY_FILE", tmp_path / "note_categories.json")
    note_router._recent_cache.clear()
    yield notes
    note_router._recent_cache.clear()


def test_recent_rows_get_category_fields(env):
    data = _data(note_router.get_recent_tasks())
    row = data["tasks"][0]
    assert row["task_id"] == "t1"
    assert row["category"] is None
    assert row["category_name"] is None

    store = nc.note_category_store()
    cat = store.create("学习")
    store.assign(["t1"], cat["id"])

    row = _data(note_router.get_recent_tasks())["tasks"][0]
    assert row["category"] == cat["id"]
    assert row["category_name"] == "学习"


def test_version_changes_when_category_changes(env):
    v1 = _data(note_router.get_recent_tasks())["version"]
    assert _data(note_router.get_recent_tasks(since=v1)).get("unchanged") is True

    cat = nc.note_category_store().create("工作")
    payload = _data(note_router.get_recent_tasks(since=v1))
    assert not payload.get("unchanged")
    v2 = payload["version"]
    assert v2 != v1

    nc.note_category_store().assign(["t1"], cat["id"])
    payload = _data(note_router.get_recent_tasks(since=v2))
    assert not payload.get("unchanged")
    assert payload["tasks"][0]["category_name"] == "工作"
    v3 = payload["version"]
    assert _data(note_router.get_recent_tasks(since=v3)).get("unchanged") is True

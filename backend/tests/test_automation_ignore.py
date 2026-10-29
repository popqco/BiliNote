"""自动化忽略名单（删除墓碑）单测。

用户诉求（2026-10-29）：删掉某条自动化笔记后，视频还在「稍后再看」里就会
被下一轮重新生成——删除必须留下持久化的「不要再生成」记录。
"""
import pathlib
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services import automation_scheduler as sched  # noqa: E402
from app.services import automation_ignore as ai  # noqa: E402
from app.services.automation_ignore import AutomationIgnoreStore, ignore_key  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return AutomationIgnoreStore(tmp_path / "automation_ignore.json")


def test_add_contains_normalizes_multi_p(store):
    store.add("BV1xx_p1", "bilibili", title="t", task_id="tid")
    assert store.contains("BV1xx", "bilibili")
    # 多 P 的 _pN 后缀归一化后是同一个键（自动化按裸 bvid 查）
    assert store.contains("BV1xx_p2", "bilibili")
    assert store.contains("BV1xx", "BILIBILI")
    assert not store.contains("BV1xx", "youtube")
    assert not store.contains("", "bilibili")
    assert ignore_key("BV1xx_p1", "bilibili") == "bilibili:BV1xx"


def test_items_sorted_and_persisted(tmp_path):
    path = tmp_path / "automation_ignore.json"
    s1 = AutomationIgnoreStore(path)
    s1.add("BV1", title="a")
    time.sleep(0.002)
    s1.add("BV2", title="b")
    items = s1.items()
    assert [i["video_id"] for i in items] == ["BV2", "BV1"]
    assert items[0]["title"] == "b" and items[0]["reason"] == "manual_delete"

    s2 = AutomationIgnoreStore(path)
    assert s2.contains("BV1", "bilibili")
    assert s2.contains("BV2", "bilibili")
    assert s1.version() == s2.version()


def test_remove_and_clear(store):
    store.add("BV1")
    store.add("BV2")
    assert store.remove("BV1") is True
    assert store.remove("BV1") is False
    assert store.keys() == {"bilibili:BV2"}
    assert store.clear() == 1
    assert store.keys() == set()
    assert store.clear() == 0


def test_empty_video_id_is_ignored(store):
    assert store.add("") is None
    assert store.items() == []


def test_cap_drops_oldest(store, monkeypatch):
    monkeypatch.setattr(ai, "MAX_ITEMS", 3)
    for i in range(5):
        store.add(f"BV{i}", title=f"t{i}")
        time.sleep(0.002)
    rows = store.items()
    assert len(rows) == 3
    assert {r["video_id"] for r in rows} == {"BV2", "BV3", "BV4"}


# ---------------------------------------------------------------------------
# 调度器：命中忽略名单的视频不提交
# ---------------------------------------------------------------------------

def _cfg():
    return {
        "enabled": True,
        "interval_minutes": 3,
        "mode": "all",
        "max_per_round": 10,
        "retry_cooldown_minutes": 30,
        "gen": {
            "model_name": "m",
            "provider_id": "p",
            "video_understanding": False,
            "quality": "medium",
            "video_interval": 6,
            "grid_size": [2, 2],
        },
        "notify": {"smtp": {"enabled": False}, "min_interval_minutes": 30},
    }


def _items(*bvids):
    return [
        {
            "bvid": b,
            "title": f"t-{b}",
            "video_url": f"https://www.bilibili.com/video/{b}",
            "add_at": time.time(),
            "cover_url": "",
            "duration": 60,
        }
        for b in bvids
    ]


@pytest.fixture
def sched_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "STATE_FILE", tmp_path / "automation_state.json")
    monkeypatch.setattr(sched, "LOCK_FILE", tmp_path / "automation.lock")
    monkeypatch.setattr(sched, "get_task_by_video", lambda bv, pf: None)
    monkeypatch.setattr(sched, "find_active_task_by_video", lambda vid: None)
    ignore = AutomationIgnoreStore(tmp_path / "automation_ignore.json")
    monkeypatch.setattr(sched, "automation_ignore_store", lambda: ignore)
    return sched.AutomationScheduler(), ignore


def test_ignored_video_is_skipped(sched_env):
    s, ignore = sched_env
    ignore.add("BV1ignored", "bilibili", title="删过的")

    submitted_bvids = []

    def fake_wait(submitted, skipped, started, cfg=None):
        for e in submitted:
            e["status"] = "SUCCESS"
        return {"submitted": submitted, "skipped": skipped}

    def fake_submit(item, cfg):
        submitted_bvids.append(item["bvid"])
        return f"tid-{item['bvid']}"

    with patch.object(sched, "fetch_watchlater", return_value=_items("BV1ignored", "BV1new")), \
            patch.object(sched.AutomationScheduler, "_submit_task", side_effect=fake_submit), \
            patch.object(sched.AutomationScheduler, "_wait_and_summarize", side_effect=fake_wait):
        result = s.run_round_once(_cfg())

    assert submitted_bvids == ["BV1new"]
    assert any(
        s["bvid"] == "BV1ignored" and "忽略" in s["reason"] for s in result["skipped"]
    )


def test_removed_from_ignore_is_considered_again(sched_env):
    s, ignore = sched_env
    ignore.add("BV1x", "bilibili")
    ignore.remove("BV1x")

    submitted_bvids = []

    def fake_wait(submitted, skipped, started, cfg=None):
        for e in submitted:
            e["status"] = "SUCCESS"
        return {"submitted": submitted, "skipped": skipped}

    with patch.object(sched, "fetch_watchlater", return_value=_items("BV1x")), \
            patch.object(sched.AutomationScheduler, "_submit_task",
                         side_effect=lambda it, c: submitted_bvids.append(it["bvid"]) or "tid"), \
            patch.object(sched.AutomationScheduler, "_wait_and_summarize", side_effect=fake_wait):
        s.run_round_once(_cfg())

    assert submitted_bvids == ["BV1x"]


def test_submit_task_applies_default_category(tmp_path, monkeypatch):
    """自动化默认分类：提交后立刻归类（分类不存在时 no-op，不影响提交）。"""
    from types import SimpleNamespace

    from app.services import note_categories as nc

    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setattr(sched, "NOTE_OUTPUT_DIR", d)
    monkeypatch.setattr(nc, "CATEGORY_FILE", tmp_path / "note_categories.json")
    monkeypatch.setattr(sched, "note_task_service", SimpleNamespace(run=lambda *a, **k: None))

    cat = nc.note_category_store().create("稍后再看")
    cfg = _cfg()
    cfg["gen"]["category_id"] = cat["id"]
    item = _items("BV1cat")[0]

    task_id = sched.AutomationScheduler()._submit_task(item, cfg)
    assert nc.note_category_store().assignments() == {task_id: cat["id"]}
    assert (d / f"{task_id}.status.json").exists()

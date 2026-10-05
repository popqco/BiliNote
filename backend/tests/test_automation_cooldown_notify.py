"""自动化调度器三道防线：ffmpeg 预检 / 失败冷却去重 / 通知节流。

背景（2026-10-05 实测）：后台进程若从未跑过健康轮询，FFMPEG_BIN_PATH 不会
前置进进程 PATH，开启视频理解的自动化任务全部以 WinError 2 失败；失败任务
不落库 → 每轮去重失效 → 同一视频被反复提交、每轮各发一封邮件（约 30 轮）。
"""
import json
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from app.services import automation_scheduler as sched


def _cfg(**over):
    cfg = {
        "enabled": True,
        "interval_minutes": 3,
        "mode": "all",
        "max_per_round": 5,
        "retry_cooldown_minutes": 30,
        "gen": {
            "model_name": "m",
            "provider_id": "p",
            "video_understanding": True,
            "quality": "medium",
            "video_interval": 6,
            "grid_size": [2, 2],
        },
        "notify": {"smtp": {"enabled": True}, "min_interval_minutes": 30},
    }
    cfg.update(over)
    return cfg


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
    # 去重查询不碰真实 DB / note_results（单测只关心冷却与节流逻辑）
    monkeypatch.setattr(sched, "get_task_by_video", lambda bv, pf: None)
    monkeypatch.setattr(sched, "find_active_task_by_video", lambda vid: None)
    return sched.AutomationScheduler(), tmp_path


def _run(s, cfg, items, statuses):
    """跑一轮：fetch 返回 items，_wait_and_summarize 按提交顺序赋终态。"""

    def fake_wait(submitted, skipped, started, cfg=None):
        for entry, st in zip(submitted, statuses):
            entry["status"] = st
            entry["message"] = f"msg-{st}"
        return {"submitted": submitted, "skipped": skipped}

    with patch.object(sched, "fetch_watchlater", return_value=list(items)), patch.object(
        sched.AutomationScheduler, "_submit_task", side_effect=lambda it, c: f"tid-{it['bvid']}"
    ), patch.object(
        sched.AutomationScheduler, "_wait_and_summarize", side_effect=fake_wait
    ):
        return s.run_round_once(cfg)


def _state(tmp_path):
    return json.loads((tmp_path / "automation_state.json").read_text(encoding="utf-8"))


def test_ffmpeg_missing_aborts_round_before_submit(sched_env):
    s, _tmp = sched_env
    with patch.object(sched, "fetch_watchlater", return_value=_items("BV1abc")), patch.object(
        sched, "check_ffmpeg_exists", return_value=False
    ), patch.object(sched.AutomationScheduler, "_submit_task") as sub:
        result = s.run_round_once(_cfg())
    assert "ffmpeg" in result.get("error", "")
    sub.assert_not_called()


def test_ffmpeg_ok_round_proceeds(sched_env):
    s, _tmp = sched_env
    result = _run(s, _cfg(), _items("BV1abc"), ["SUCCESS"])
    assert [e["task_id"] for e in result["submitted"]] == ["tid-BV1abc"]


def test_failed_video_enters_cooldown_and_is_skipped(sched_env):
    s, tmp = sched_env
    _run(s, _cfg(), _items("BV1abc"), ["FAILED"])
    assert _state(tmp)["failures"]["BV1abc"]["count"] == 1

    result = _run(s, _cfg(), _items("BV1abc"), ["FAILED"])
    assert result["submitted"] == []
    assert any("冷却" in sk["reason"] for sk in result["skipped"])
    # 冷却期内不重复提交，失败计数也不增长
    assert _state(tmp)["failures"]["BV1abc"]["count"] == 1


def test_success_clears_cooldown(sched_env):
    s, tmp = sched_env
    _run(s, _cfg(), _items("BV1abc"), ["FAILED"])
    state = _state(tmp)
    state["failures"]["BV1abc"]["ts"] = time.time() - 31 * 60  # 拨出 30 分钟冷却窗
    (tmp / "automation_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    result = _run(s, _cfg(), _items("BV1abc"), ["SUCCESS"])
    assert len(result["submitted"]) == 1
    assert "BV1abc" not in _state(tmp).get("failures", {})


def test_cooldown_disabled_when_zero(sched_env):
    s, _tmp = sched_env
    _run(s, _cfg(retry_cooldown_minutes=0), _items("BV1abc"), ["FAILED"])
    result = _run(s, _cfg(retry_cooldown_minutes=0), _items("BV1abc"), ["FAILED"])
    assert len(result["submitted"]) == 1  # 无冷却：每轮照常重试


def _patch_status_reader(s, monkeypatch, status="SUCCESS"):
    # _wait_and_summarize 的终态判定读状态文件而非入参 dict；单测里直接短路
    monkeypatch.setattr(s, "_read_status", lambda tid: {"status": status})


def test_notify_throttled_within_min_interval(sched_env, monkeypatch):
    s, tmp = sched_env
    _patch_status_reader(s, monkeypatch)
    s._update_state(last_notify_ts=time.time() - 5 * 60)  # 刚发过 5 分钟
    sent = []

    def fake_send(result):
        sent.append(result)
        return [{"channel": "x", "ok": True, "detail": "ok"}]

    with patch.object(sched, "send_summary", side_effect=fake_send):
        result = s._wait_and_summarize(
            [{"task_id": "t", "bvid": "BV1abc", "status": "SUCCESS", "title": "t"}],
            [],
            datetime.now(),
            _cfg(),
        )
    assert sent == []
    assert result["notify"][0]["channel"] == "（节流）"


def test_notify_sent_after_interval_and_timestamped(sched_env, monkeypatch):
    s, tmp = sched_env
    _patch_status_reader(s, monkeypatch)
    s._update_state(last_notify_ts=time.time() - 3600)

    with patch.object(sched, "send_summary", return_value=[{"channel": "x", "ok": True, "detail": "ok"}]) as m:
        s._wait_and_summarize(
            [{"task_id": "t", "bvid": "BV1abc", "status": "SUCCESS", "title": "t"}],
            [],
            datetime.now(),
            _cfg(),
        )
    assert m.called
    last_ts = _state(tmp)["last_notify_ts"]
    assert time.time() - last_ts < 60


def test_notify_throttle_disabled_when_zero(sched_env, monkeypatch):
    s, _tmp = sched_env
    _patch_status_reader(s, monkeypatch)
    s._update_state(last_notify_ts=time.time())

    with patch.object(sched, "send_summary", return_value=[{"channel": "x", "ok": True, "detail": "ok"}]) as m:
        s._wait_and_summarize(
            [{"task_id": "t", "bvid": "BV1abc", "status": "SUCCESS", "title": "t"}],
            [],
            datetime.now(),
            _cfg(notify={"smtp": {"enabled": True}, "min_interval_minutes": 0}),
        )
    assert m.called

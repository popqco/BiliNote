"""NoteTaskService.submit 的提交语义回归测试。"""
import json

import pytest

from app.enmus.note_enums import DownloadQuality
from app.services import note_tasks as note_tasks_mod
from app.services import task_state
from app.services.note_tasks import NoteTaskError, NoteTaskService
from app.services.task_state import runs


@pytest.fixture(autouse=True)
def _clean_runs():
    runs.reset_for_tests()
    yield
    runs.reset_for_tests()


@pytest.fixture
def ready(monkeypatch):
    class _Ready:
        def is_model_ready(self):
            return {
                "ready": True,
                "transcriber_type": "fast-whisper",
                "model_size": "tiny",
                "downloading": False,
                "reason": "",
            }

    monkeypatch.setattr(note_tasks_mod, "TranscriberConfigManager", _Ready)
    # 元信息预取线程不联网
    monkeypatch.setattr(note_tasks_mod, "fetch_video_meta", lambda *a, **k: {})


def _submit(service, tmp_path, **overrides):
    kwargs = dict(
        video_url="https://www.bilibili.com/video/BV1xx411c7mD",
        platform="bilibili",
        quality=DownloadQuality.medium,
        model_name="m",
        provider_id="p",
        schedule=lambda fn, *a, **k: None,
        output_dir=tmp_path,
    )
    kwargs.update(overrides)
    return service.submit(**kwargs)


def test_readiness_gate_raises(monkeypatch, tmp_path):
    class _NotReady:
        def is_model_ready(self):
            return {
                "ready": False,
                "transcriber_type": "fast-whisper",
                "model_size": "tiny",
                "downloading": False,
                "reason": "转写模型尚未下载",
            }

    monkeypatch.setattr(note_tasks_mod, "TranscriberConfigManager", _NotReady)

    with pytest.raises(NoteTaskError) as exc:
        _submit(NoteTaskService(), tmp_path)

    assert exc.value.code == 300102
    assert exc.value.data["reason"] == "transcriber_model_not_ready"


def test_missing_model_rejected(ready, tmp_path):
    with pytest.raises(NoteTaskError) as exc:
        _submit(NoteTaskService(), tmp_path, model_name=None, provider_id=None)

    assert exc.value.code == 400
    assert "请选择模型" in exc.value.message


def test_retry_conflict_rejected(ready, tmp_path):
    task_state.mark_task_active("t-busy", "BV1")

    with pytest.raises(NoteTaskError) as exc:
        _submit(NoteTaskService(), tmp_path, task_id="t-busy")

    assert exc.value.data["duplicated"] is True
    assert exc.value.data["existing_task_id"] == "t-busy"


def test_single_submit_writes_pending_and_schedules(ready, tmp_path, monkeypatch):
    monkeypatch.setattr(note_tasks_mod, "extract_video_id", lambda url, platform: "BV1xx411c7mD")
    scheduled = []
    service = NoteTaskService()

    result = _submit(
        service, tmp_path,
        formats=["toc", "summary"], style="minimal", extras="e",
        schedule=lambda fn, *a, **k: scheduled.append((fn, a, k)),
    )

    assert result.task_id
    data = json.loads((tmp_path / f"{result.task_id}.status.json").read_text(encoding="utf-8"))
    assert data["status"] == "PENDING"
    assert data["video_id"] == "BV1xx411c7mD"
    assert data["request"]["format"] == ["toc", "summary"]
    assert data["request"]["extras"] == "e"
    assert len(scheduled) == 1
    assert scheduled[0][0] == service.run
    assert scheduled[0][2]["output_dir"] == tmp_path


def test_local_submit_without_video_id_still_gets_task_id(ready, tmp_path, monkeypatch):
    """local 平台 extract_video_id 返回 None，也必须生成 task_id（历史 500 回归）。"""
    monkeypatch.setattr(note_tasks_mod, "extract_video_id", lambda url, platform: None)

    result = _submit(
        NoteTaskService(), tmp_path,
        video_url="/uploads/test.mp4", platform="local",
    )

    assert result.task_id
    data = json.loads((tmp_path / f"{result.task_id}.status.json").read_text(encoding="utf-8"))
    assert data["status"] == "PENDING"
    assert data["platform"] == "local"


def test_watchlater_batch_returns_created(ready, tmp_path, monkeypatch):
    from app.services import watchlater

    monkeypatch.setattr(watchlater, "is_watchlater_list_url", lambda url: True)
    monkeypatch.setattr(
        watchlater, "fetch_watchlater",
        lambda max_items=50: [
            {"bvid": "BV1", "video_url": "https://b/v/BV1", "title": "a"},
            {"bvid": "BV2", "video_url": "https://b/v/BV2", "title": "b"},
        ],
    )
    service = NoteTaskService()
    monkeypatch.setattr(service, "run", lambda *a, **k: None)

    result = _submit(service, tmp_path, video_url="https://www.bilibili.com/watchlater/list")

    assert result.created is not None
    assert len(result.created) == 2
    assert result.as_response()["batch"] is True
    assert result.as_response()["count"] == 2


def test_prefetched_transcript_bypasses_readiness(monkeypatch, tmp_path):
    class _NotReady:
        def is_model_ready(self):
            raise AssertionError("有预取字幕时不应检查本地模型")

    monkeypatch.setattr(note_tasks_mod, "TranscriberConfigManager", _NotReady)
    monkeypatch.setattr(note_tasks_mod, "extract_video_id", lambda url, platform: "BV1")
    monkeypatch.setattr(note_tasks_mod, "fetch_video_meta", lambda *a, **k: {})

    result = _submit(
        NoteTaskService(), tmp_path,
        prefetched_transcript={
            "language": "zh",
            "full_text": "你好世界",
            "segments": [{"start": 0, "end": 1, "text": "你好世界"}],
        },
    )

    cache = tmp_path / f"{result.task_id}_transcript.json"
    assert cache.exists()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["full_text"] == "你好世界"
    assert payload["segments"][0]["text"] == "你好世界"

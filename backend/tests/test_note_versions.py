import json
import os
import time
from pathlib import Path

from app.services import task_state
from app.services.note_versions import (
    archive_previous_result,
    is_archived,
    list_task_versions,
)


def _write_result(root: Path, task_id: str, markdown: str, **extra):
    payload = {
        "markdown": markdown,
        "audio_meta": {"title": "t", "video_id": extra.pop("video_id", "vid-1")},
        "model_name": extra.pop("model_name", "m1"),
        "style": extra.pop("style", "minimal"),
    }
    payload.update(extra)
    path = root / f"{task_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_archive_previous_result_and_list(tmp_path):
    tid = "task-1"
    old = _write_result(tmp_path, tid, "old markdown")
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert archive_previous_result(tid, tmp_path, "new markdown") is True
    versions_file = tmp_path / f"{tid}_versions.json"
    assert versions_file.exists()
    archived = json.loads(versions_file.read_text(encoding="utf-8"))["versions"]
    assert [v["content"] for v in archived] == ["old markdown"]

    # 同内容不重复归档
    assert archive_previous_result(tid, tmp_path, "new markdown") is False

    _write_result(tmp_path, tid, "new markdown")
    all_versions = list_task_versions(tid, tmp_path)
    contents = [v["content"] for v in all_versions]
    assert "new markdown" in contents
    assert "old markdown" in contents
    # 新版时间更新，排在前面
    assert contents[0] == "new markdown"


def test_is_archived_only_checks_versions_file(tmp_path):
    tid = "task-a"
    _write_result(tmp_path, tid, "current")
    # 当前结果不算归档
    assert is_archived(tid, tmp_path, "current") is False
    assert archive_previous_result(tid, tmp_path, "new") is True
    assert is_archived(tid, tmp_path, "current") is True
    assert is_archived(tid, tmp_path, "missing") is False


def test_list_versions_includes_same_video_old_task(tmp_path):
    tid = "task-new"
    old_tid = "task-old"
    _write_result(tmp_path, tid, "current", video_id="vid-42")
    _write_result(tmp_path, old_tid, "legacy version", video_id="vid-42")
    task_state.write_status(
        tid, "SUCCESS", extra={"video_id": "vid-42", "platform": "bilibili"}, root=tmp_path
    )
    task_state.write_status(
        old_tid, "SUCCESS", extra={"video_id": "vid-42", "platform": "bilibili"}, root=tmp_path
    )
    versions = list_task_versions(tid, tmp_path)
    contents = [v["content"] for v in versions]
    assert "current" in contents
    assert "legacy version" in contents
    assert len({v["ver_id"] for v in versions}) == len(versions)


def test_list_versions_dedupes_identical_content(tmp_path):
    tid = "task-x"
    _write_result(tmp_path, tid, "same")
    task_state.write_status(
        tid, "SUCCESS", extra={"video_id": "vid-x", "platform": "bilibili"}, root=tmp_path
    )
    archive_previous_result(tid, tmp_path, "new")
    # 手工再塞一条同内容归档，模拟重复写入
    path = tmp_path / f"{tid}_versions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["versions"].append(dict(data["versions"][0]))
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    versions = list_task_versions(tid, tmp_path)
    assert sum(1 for v in versions if v["content"] == "same") == 1

"""task_state 深模块的语义回归测试。"""
import json
import os
import time

from app.enmus.task_status_enums import TaskStatus
from app.services import task_state
from app.services.task_state import runs


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_write_status_merges_metadata(tmp_path):
    task_state.write_status(
        "t-merge", TaskStatus.PENDING,
        extra={"video_id": "BV1", "origin": "manual", "audio_meta": {"title": "旧标题"}},
        root=tmp_path,
    )
    task_state.write_status(
        "t-merge", TaskStatus.SUMMARIZING, message="总结中",
        root=tmp_path,
    )

    data = json.loads((tmp_path / "t-merge.status.json").read_text(encoding="utf-8"))
    assert data["status"] == "SUMMARIZING"
    assert data["message"] == "总结中"
    assert data["video_id"] == "BV1"
    assert data["audio_meta"]["title"] == "旧标题"


def test_superseded_run_writes_nothing(tmp_path):
    runs.reset_for_tests()
    old = runs.register_run("t-sup")
    runs.register_run("t-sup")

    task_state.write_status("t-sup", TaskStatus.CANCELLED, run=old, root=tmp_path)

    assert not (tmp_path / "t-sup.status.json").exists()


def test_cancelled_run_only_allows_cancelled(tmp_path):
    runs.reset_for_tests()
    run = runs.register_run("t-cancel")
    task_state.write_status("t-cancel", TaskStatus.PARSING, run=run, root=tmp_path)
    runs.request_cancel_task("t-cancel")

    task_state.write_status("t-cancel", TaskStatus.SUMMARIZING, run=run, root=tmp_path)
    data = json.loads((tmp_path / "t-cancel.status.json").read_text(encoding="utf-8"))
    assert data["status"] == "PARSING"

    task_state.write_status("t-cancel", TaskStatus.CANCELLED, run=run, root=tmp_path)
    data = json.loads((tmp_path / "t-cancel.status.json").read_text(encoding="utf-8"))
    assert data["status"] == "CANCELLED"


def test_recent_dedupes_by_video(tmp_path):
    _write(tmp_path / "a.status.json", {
        "status": "FAILED", "video_id": "BV1", "message": "old",
    })
    _write(tmp_path / "b.status.json", {
        "status": "SUCCESS", "video_id": "BV1", "audio_meta": {"title": "新标题"},
    })
    _write(tmp_path / "c.status.json", {
        "status": "SUCCESS", "video_id": "BV2", "audio_meta": {"title": "另一个"},
    })

    items = task_state.list_recent_tasks(limit=80, root=tmp_path)

    assert len(items) == 2
    by_video = {item["video_id"]: item for item in items}
    assert by_video["BV1"]["status"] == "SUCCESS"
    assert by_video["BV1"]["title"] == "新标题"


def test_purge_only_removes_task_prefix(tmp_path, monkeypatch):
    _write(tmp_path / "t-del.status.json", {"status": "SUCCESS"})
    _write(tmp_path / "t-del.json", {"markdown": "x"})
    _write(tmp_path / "t-del_audio.json", {"title": "x"})
    _write(tmp_path / "keep.json", {"markdown": "keep"})

    import app.services.vector_store as vector_store

    class _NoopVectorStore:
        def delete_index(self, task_id):
            pass

    monkeypatch.setattr(vector_store, "VectorStoreManager", _NoopVectorStore)

    result = task_state.purge_task("t-del", root=tmp_path)

    assert result["deleted"] == 3
    assert not (tmp_path / "t-del.status.json").exists()
    assert not (tmp_path / "t-del.json").exists()
    assert (tmp_path / "keep.json").exists()


def test_reap_only_touches_stale_in_progress(tmp_path):
    old = tmp_path / "old.status.json"
    recent = tmp_path / "recent.status.json"
    done = tmp_path / "done.status.json"
    _write(old, {"status": "PENDING"})
    _write(recent, {"status": "SUMMARIZING"})
    _write(done, {"status": "SUCCESS"})
    past = time.time() - 120
    os.utime(old, (past, past))
    os.utime(done, (past, past))

    reaped = task_state.reap_interrupted_tasks(time.time(), root=tmp_path)

    assert reaped == 1
    assert json.loads(old.read_text(encoding="utf-8"))["status"] == "FAILED"
    assert json.loads(recent.read_text(encoding="utf-8"))["status"] == "SUMMARIZING"
    assert json.loads(done.read_text(encoding="utf-8"))["status"] == "SUCCESS"


def test_active_registry_token_ownership():
    runs.reset_for_tests()
    old = runs.register_run("t-active")
    runs.mark_task_active("t-active", "BV1", token=old)
    new = runs.register_run("t-active")
    # 重试接管：路由会用新 run 覆盖活跃登记的 token
    runs.mark_task_active("t-active", "BV1", token=new)

    runs.mark_task_done("t-active", token=old)
    assert runs.is_task_active("t-active"), "旧 run 收尾不能摘掉新 run 的登记"

    runs.mark_task_done("t-active", token=new)
    assert not runs.is_task_active("t-active")

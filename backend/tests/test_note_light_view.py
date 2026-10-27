"""笔记阅读轻量视图与 /tasks/recent 版本缓存回归测试。

远端手机打开笔记只需要 markdown；完整结果里的 transcript / audio_meta.raw_info
占了大头。`view=note` 必须是纯加法：默认 full 一个字段都不能少，旧客户端
（MCP、导出、聊天索引）仍走完整结果。
"""
import json

import app.routers.note as note_router


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_result(tmp_path, task_id="t-light"):
    _write(tmp_path / f"{task_id}.status.json", {
        "status": "SUCCESS",
        "video_id": "BV1",
        "platform": "bilibili",
        "audio_meta": {"title": "旧标题"},
    })
    _write(tmp_path / f"{task_id}.json", {
        "markdown": "# 正文\n\n这是正文。",
        "model_name": "m",
        "provider_id": "p",
        "style": "minimal",
        "transcript": {
            "full_text": "全文" * 100,
            "language": "zh",
            "segments": [{"start": 0, "end": 1, "text": "第一段"}],
            "raw": {"huge": "不应出现在轻量视图"},
        },
        "audio_meta": {
            "title": "标题",
            "cover_url": "http://cover",
            "duration": 12.3,
            "platform": "bilibili",
            "video_id": "BV1",
            "raw_info": {"uploader": "UP主", "webpage_url": "http://v", "huge": "x" * 5000},
        },
    })


def _payload(resp):
    return json.loads(resp.body.decode("utf-8"))


def test_note_view_strips_transcript_and_raw_info(tmp_path, monkeypatch):
    _seed_result(tmp_path)
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)

    data = _payload(note_router.get_task_status("t-light", view="note"))["data"]
    result = data["result"]

    assert result["markdown"].startswith("# 正文")
    assert "transcript" not in result
    assert result["audio_meta"]["title"] == "标题"
    assert result["audio_meta"]["uploader"] == "UP主"
    assert result["audio_meta"]["webpage_url"] == "http://v"
    assert "raw_info" not in result["audio_meta"]


def test_full_view_still_has_transcript_and_raw_info(tmp_path, monkeypatch):
    _seed_result(tmp_path)
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)

    result = _payload(note_router.get_task_status("t-light"))["data"]["result"]

    assert result["transcript"]["segments"][0]["text"] == "第一段"
    assert result["audio_meta"]["raw_info"]["uploader"] == "UP主"


def test_failed_with_result_is_projected_too(tmp_path, monkeypatch):
    _seed_result(tmp_path)
    _write(tmp_path / "t-light.status.json", {
        "status": "FAILED",
        "message": "总结失败",
        "audio_meta": {"title": "标题"},
    })
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)

    data = _payload(note_router.get_task_status("t-light", view="note"))["data"]

    assert data["status"] == "FAILED"
    assert data["result"]["markdown"].startswith("# 正文")
    assert "transcript" not in data["result"]


def test_transcript_endpoint_returns_only_needed_fields(tmp_path, monkeypatch):
    _seed_result(tmp_path)
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)

    data = _payload(note_router.get_task_transcript("t-light"))["data"]

    assert data["transcript"]["segments"][0]["text"] == "第一段"
    assert data["transcript"]["full_text"]
    assert "raw" not in data["transcript"]


def test_transcript_endpoint_404_when_no_result(tmp_path, monkeypatch):
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)

    resp = note_router.get_task_transcript("missing")
    # 与其它业务错误一致走 HTTP 200 + 业务 code 信封
    assert _payload(resp)["code"] == 404


def test_recent_version_short_circuits_when_unchanged(tmp_path, monkeypatch):
    _seed_result(tmp_path)
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", tmp_path)
    note_router._recent_cache.clear()

    first = _payload(note_router.get_recent_tasks(limit=10))["data"]
    version = first["version"]
    assert first["tasks"]
    assert "unchanged" not in first

    same = _payload(note_router.get_recent_tasks(limit=10, since=version))["data"]
    assert same["unchanged"] is True
    assert same["tasks"] == []
    assert same["version"] == version

    _write(tmp_path / "another.status.json", {"status": "SUCCESS", "video_id": "BV2"})
    changed = _payload(note_router.get_recent_tasks(limit=10, since=version))["data"]
    assert changed["version"] != version
    assert changed["tasks"]

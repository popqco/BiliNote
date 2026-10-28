"""存量截图维护的密度语义（2026-10-28）。

不直接跑会重写用户数据的端点，只测纯函数：
- sparse/normal/rich（旧笔记）→ 按该预设；
- global/adaptive/没有 density → 用结果里的 screenshot_interval 快照；
- 都没有才用内部自适应基准（90s / 60 张）。
"""
from app.routers.maintenance import _cap_for, _note_interval


def test_note_interval_semantics():
    # global/adaptive 不再跟“当前全局”，用生成时的快照
    assert _note_interval(
        {"screenshot_density": "global", "screenshot_interval": 30}, 90
    ) == 30
    assert _note_interval(
        {"screenshot_density": "adaptive", "screenshot_interval": 120}, 90
    ) == 120
    # 显式预设的旧笔记仍按预设
    assert _note_interval(
        {"screenshot_density": "sparse", "screenshot_interval": 30}, 90
    ) == 180
    assert _note_interval({"screenshot_density": "normal"}, 30) == 90
    # 旧结果：用生成时的快照
    assert _note_interval({"screenshot_interval": 45}, 90) == 45
    assert _note_interval({}, 90) == 90


def test_cap_for_uses_per_note_density():
    duration = {"audio_meta": {"duration": 600}}
    # global + 当前全局 30s → 600/30 = 20
    assert _cap_for({**duration, "screenshot_density": "global"}, 30, 100) == 20
    # 显式 sparse → 600/180 = 4（不受 fallback 30 影响）
    assert _cap_for({**duration, "screenshot_density": "sparse"}, 30, 100) == 4
    # 旧结果没有 density → 用快照 45s
    assert _cap_for({**duration, "screenshot_interval": 45}, 30, 100) == 14


def _write_note(root, task_id, markdown, duration=600):
    import json

    payload = {
        "markdown": markdown,
        "audio_meta": {"title": task_id, "duration": duration},
        "model_name": "m",
        "provider_id": "p",
    }
    path = root / f"{task_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_screenshot_slim_rewrites_with_archive_and_cleans_residue(tmp_path, monkeypatch):
    import json

    from app.routers import maintenance

    root = tmp_path / "notes"
    root.mkdir()
    monkeypatch.setattr(maintenance, "NOTE_OUTPUT_DIR", root)
    monkeypatch.setattr(maintenance, "SCREENSHOTS_DIR", tmp_path / "shots")

    images = "\n".join(
        f"![](/static/screenshots/screenshot_{i:03}_x.jpg)" for i in range(10)
    )
    over = "## 一节\n\n- \n" + images + "\n"
    under = "## 一节\n\n![](/static/screenshots/one.jpg)\n- ：说明文字\n"
    _write_note(root, "task-over", over)
    _write_note(root, "task-under", under)

    # min_images 是全局过滤条件，残渣清理也必须遵守：≥100 时两篇都不该被碰
    dry = maintenance.screenshot_slim(
        maintenance.SlimRequest(
            dry_run=True, min_images=100, interval=30, max_count=5
        )
    )
    dry_data = json.loads(dry.body.decode())["data"]
    assert dry_data["notes_scanned"] == 0
    assert dry_data["residue_cleaned"] == 0

    res = maintenance.screenshot_slim(
        maintenance.SlimRequest(
            dry_run=False, min_images=0, interval=30, max_count=5
        )
    )
    data = json.loads(res.body.decode())["data"]

    assert data["notes_changed"] == 1
    assert data["images_before"] == 10
    assert data["images_after"] == 5
    assert data["residue_cleaned"] == 1
    assert data["skipped_archive"] == 0

    new_over = json.loads((root / "task-over.json").read_text(encoding="utf-8"))["markdown"]
    assert new_over.count("/static/screenshots/") == 5
    assert "\n- \n" not in new_over
    # 旧正文已归档，可切回
    archived = json.loads(
        (root / "task-over_versions.json").read_text(encoding="utf-8")
    )["versions"]
    assert any(v["content"] == over.strip() for v in archived)
    # markdown 旁路文件同步更新
    assert (root / "task-over_markdown.md").read_text(encoding="utf-8") == new_over

    new_under = json.loads(
        (root / "task-under.json").read_text(encoding="utf-8")
    )["markdown"]
    assert "- 说明文字" in new_under
    assert "- ：说明文字" not in new_under

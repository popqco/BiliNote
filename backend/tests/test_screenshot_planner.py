"""自适应截图编排（Phase-1，2026-10-28）单测。

覆盖：
- 预算换算/最小间隔/覆盖下限/每小节上限/补齐；
- 密集段比稀疏段拿到更多截图（核心用户诉求）；
- 采样帧特征（dHash 变化、边缘密度、清晰度）与 ±2s 吸附；
- planner 异常/影子模式在 note._insert_screenshots 里的回退语义；
- screenshot_planner 枚举配置的读写与非法值回退。
"""
from pathlib import Path

import pytest

# 同 test_screenshot_policy：收集期可能被 test_video_reader_dedupe 换成假 PIL，
# 这里在本模块导入时抓住真货，并在图像测试里注回 planner 模块。
try:
    from PIL import Image as _RealPILImage
    from PIL import ImageDraw as _RealImageDraw
except Exception:  # pragma: no cover - 无 PIL 环境
    _RealPILImage = None
    _RealImageDraw = None

from app.services import note as note_mod
from app.utils import screenshot_planner as sp
from app.utils.screenshot_marker import iter_screenshot_markers
from app.utils.screenshot_policy import section_spans, select_markers


def _markdown(timestamps, section_every=None):
    parts = ["## 第一节\n"]
    for idx, ts in enumerate(timestamps):
        if section_every and idx and idx % section_every == 0:
            parts.append(f"\n## 第{idx // section_every + 1}节\n")
        parts.append(f"*Screenshot-[{ts // 60:02d}:{ts % 60:02d}]*\n图注：t{ts}\n")
    return "".join(parts)


def _plan(md, **kwargs):
    return sp.plan_shots(md, iter_screenshot_markers(md), **kwargs)


# ---------------------------------------------------------------- 纯函数


def test_total_budget_and_min_gap():
    assert sp.total_budget(3600, 90, 60) == 40
    assert sp.total_budget(3600, 90, 20) == 20       # 配置上限优先
    assert sp.total_budget(60, 90, 60) == 1          # 短视频不硬凑
    assert sp.total_budget(None, 90, 60) == 60       # 时长未知按上限
    assert sp.min_gap_for(90) == 15
    assert sp.min_gap_for(45) == 8
    assert sp.min_gap_for(180) == 30


def test_dense_segment_gets_more_shots_and_covers_all_buckets():
    dense = list(range(0, 601, 20))
    sparse = list(range(720, 3601, 120))
    md = _markdown(dense + sparse)

    result = _plan(md, duration=3600, interval=90, max_count=60)

    assert 20 <= len(result.shots) <= 40
    early = [s for s in result.shots if s.anchor_ts <= 600]
    next10 = [s for s in result.shots if 600 < s.anchor_ts <= 1200]
    assert len(early) > len(next10), (len(early), len(next10))
    # 每个有候选的 5 分钟桶至少 1 张（覆盖下限）；3600 落在第 13 个桶
    buckets = {s.anchor_ts // sp.BUCKET_SEC for s in result.shots}
    assert buckets == set(range(13))


def test_min_gap_is_respected():
    md = _markdown([100, 105, 110])
    result = _plan(md, duration=600, interval=90, max_count=10)
    assert len(result.shots) == 1


def test_section_cap_limits_one_section():
    main = list(range(0, 1000, 20))
    rest = [1000, 2000, 3000]
    md = "## 一\n" + "".join(
        f"*Screenshot-[{ts // 60:02d}:{ts % 60:02d}]*\n图注：t{ts}\n" for ts in main
    ) + "".join(
        f"\n## 其他{ts}\n*Screenshot-[{ts // 60:02d}:{ts % 60:02d}]*\n图注：t{ts}\n"
        for ts in rest
    )
    spans = section_spans(md)
    assert len(spans) == 4

    result = _plan(md, duration=3600, interval=90, max_count=60)

    first_section = sum(1 for s in result.shots if s.anchor_start < spans[1][0])
    assert first_section <= 30
    assert len(result.shots) <= 40
    # 其他小节仍然有覆盖
    assert any(s.anchor_ts >= 2000 for s in result.shots)


def test_plan_is_deterministic():
    md = _markdown(list(range(0, 1200, 45)))
    a = _plan(md, duration=1200, interval=90, max_count=30)
    b = _plan(md, duration=1200, interval=90, max_count=30)
    assert [s.ts for s in a.shots] == [s.ts for s in b.shots]
    assert a.dropped == b.dropped


def test_no_frames_still_plans_with_valid_anchors():
    md = _markdown([10, 310, 610, 910])
    result = _plan(md, duration=1000, interval=90, max_count=12)
    starts = {m.start for m in iter_screenshot_markers(md)}
    assert result.shots
    assert all(s.anchor_start in starts for s in result.shots)


# ---------------------------------------------------------------- 帧特征


def _frame_image(path, detailed: bool):
    assert _RealPILImage is not None
    im = _RealPILImage.new("L", (160, 90), 250 if detailed else 24)
    if detailed:
        draw = _RealImageDraw.Draw(im)
        for x in range(4, 156, 12):
            draw.line((x, 4, x, 86), fill=0, width=2)
        draw.rectangle((10, 10, 150, 40), outline=0, width=3)
    im.convert("RGB").save(path, quality=92)


def _use_real_pil(monkeypatch):
    if _RealPILImage is None:
        pytest.skip("环境没有 PIL")
    monkeypatch.setattr(sp, "_PILImage", _RealPILImage)


def test_frame_index_parses_long_video_names(tmp_path):
    _frame_image(tmp_path / "frame_100_52.jpg", detailed=False)
    index = sp.FrameIndex(tmp_path, interval=6)
    assert [ref.ts for ref in index.frames] == [100 * 60 + 52]


def test_frame_features_distinguish_detailed_screen(monkeypatch, tmp_path):
    _use_real_pil(monkeypatch)
    _frame_image(tmp_path / "frame_00_00.jpg", detailed=False)
    _frame_image(tmp_path / "frame_00_06.jpg", detailed=True)
    index = sp.FrameIndex(tmp_path, interval=6)
    blank, detail = index.frames

    assert index.digest(blank) is not None
    assert index.digest(detail) is not None
    assert index.digest(blank) != index.digest(detail)
    assert index.change(detail) > 0
    assert index.pixels(detail)[0] > index.pixels(blank)[0]


def test_visual_score_prefers_detailed_frame(monkeypatch, tmp_path):
    """预算只有 1 张时，选边缘/清晰度更高的那张（同样的语义标记）。"""
    _use_real_pil(monkeypatch)
    frames = tmp_path / "frames"
    frames.mkdir()
    _frame_image(frames / "frame_00_00.jpg", detailed=False)
    _frame_image(frames / "frame_00_06.jpg", detailed=True)
    md = _markdown([0, 6])

    result = _plan(
        md, frame_dir=frames, duration=60, interval=90, max_count=10
    )

    assert len(result.shots) == 1
    assert result.shots[0].ts == 6


def test_snap_only_within_two_seconds(monkeypatch, tmp_path):
    _use_real_pil(monkeypatch)
    frames = tmp_path / "frames"
    frames.mkdir()
    _frame_image(frames / "frame_00_00.jpg", detailed=False)
    _frame_image(frames / "frame_06_00.jpg", detailed=True)
    md = _markdown([300, 359])

    result = _plan(
        md, frame_dir=frames, duration=600, interval=90, max_count=10
    )

    by_anchor = {s.anchor_ts: s.ts for s in result.shots}
    assert by_anchor[359] == 360
    assert by_anchor[300] == 300


# ---------------------------------------------------------------- note 接入


def _generator(monkeypatch, tmp_path):
    created = []

    def fake_shot(video, outdir, ts, idx):
        path = tmp_path / f"shot_{idx}_{ts}.jpg"
        _RealPILImage.new("RGB", (16, 16), (idx * 8 % 255, 0, 0)).save(path)
        created.append((ts, str(path)))
        return str(path)

    monkeypatch.setattr(note_mod, "generate_screenshot", fake_shot)
    monkeypatch.setattr(note_mod, "dhash", lambda path: None)
    monkeypatch.setattr(note_mod, "IMAGE_OUTPUT_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(note_mod, "get_app_dir", lambda name: str(tmp_path / name))
    if _RealPILImage is not None:
        monkeypatch.setattr(sp, "_PILImage", _RealPILImage)

    gen = note_mod.NoteGenerator.__new__(note_mod.NoteGenerator)
    gen.screenshot_interval = 90
    gen.screenshot_max_count = 12
    gen._last_frame_interval = 6
    return gen, created


def _write_frames(tmp_path, task_id, timestamps):
    frame_dir = tmp_path / "output_frames" / task_id
    frame_dir.mkdir(parents=True)
    for ts in timestamps:
        _frame_image(frame_dir / f"frame_{ts // 60:02d}_{ts % 60:02d}.jpg", detailed=True)


def test_insert_screenshots_adaptive_keeps_plan(monkeypatch, tmp_path):
    if _RealPILImage is None:
        pytest.skip("环境没有 PIL")
    gen, created = _generator(monkeypatch, tmp_path)
    _write_frames(tmp_path, "task1", [0, 6, 12, 18])
    md = _markdown([10, 300, 600, 900])

    out = gen._insert_screenshots(md, Path("dummy.mp4"), duration=1000, task_id="task1")

    assert out.count("![") == 4
    assert out.count("图注") == 4
    assert "Screenshot-" not in out
    assert len(created) == 4


def test_planner_error_falls_back_to_legacy(monkeypatch, tmp_path):
    gen, _ = _generator(monkeypatch, tmp_path)
    md = _markdown([10, 20, 300, 600])
    markers = iter_screenshot_markers(md)
    legacy = select_markers(md, markers, duration=1000, interval=90, max_count=12)

    def boom(*args, **kwargs):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(note_mod, "plan_shots", boom)

    out = gen._insert_screenshots(md, Path("dummy.mp4"), duration=1000, task_id="task1")

    assert out.count("![") == len(legacy.keep)
    assert "Screenshot-" not in out

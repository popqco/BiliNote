r"""video_reader 时间戳解析（100+ 分钟视频）与 _adapt_frame_budget 新预算策略单测。

背景（2026-10-11）：
- 旧帧名正则 \d{2} 只认两位分钟，≥100 分钟视频排序 key 全变 inf（顺序乱）、
  拼图上时间戳消失；
- 预算默认 600/150 → 2400/500，且扩格策略从「先升 3×3」改为「优先拉间隔保
  持 2×2，间隔要超设定值 2 倍才升 3×3」（用户反馈拼图格子太小）。
"""
import pytest

from app.utils.video_reader import VideoReader, _FRAME_TS_RE
from app.services import note as note_mod
from app.services.note import NoteGenerator


@pytest.fixture
def reader(tmp_path):
    return VideoReader(
        video_path="dummy.mp4",
        frame_dir=str(tmp_path / "frames"),
        grid_dir=str(tmp_path / "grids"),
    )


def test_frame_regex_supports_three_digit_minutes(reader):
    assert reader.extract_time_from_filename("frame_101_52.jpg") == 101 * 60 + 52
    assert reader.extract_time_from_filename("frame_00_03.jpg") == 3
    assert reader.extract_time_from_filename("frame_99_59.jpg") == 99 * 60 + 59
    assert reader.extract_time_from_filename("grid_1.jpg") == float("inf")
    assert _FRAME_TS_RE.fullmatch("frame_101_52.jpg")


def test_extract_frames_accepts_legacy_max_frames_arg(reader):
    """旧调用点直接传 max_frames 的兼容性（test_video_reader_dedupe 依赖）。"""
    import re as _re
    from unittest.mock import patch as _patch

    from app.utils import video_reader as vr_mod

    def fake_run(cmd, check=True):
        out = next(a for a in cmd if isinstance(a, str) and a.endswith(".jpg"))
        m = _re.search(r"frame_(\d{2})_(\d{2})\.jpg$", out)
        sec = int(m.group(1)) * 60 + int(m.group(2))
        with open(out, "wb") as f:
            f.write(b"frame-%d" % sec)
        return 0

    # 必须打在 video_reader 模块自己 import 时绑定的 ffmpeg/subprocess 对象上：
    # test_video_reader_dedupe 在收集期会把 sys.modules["ffmpeg"] 换成假模块，
    # 按顶层 import 的对象打补丁可能落在假模块上、被 video_reader 绕开。
    with _patch.object(vr_mod.ffmpeg, "probe", return_value={"format": {"duration": "6"}}), \
            _patch.object(vr_mod.subprocess, "run", side_effect=fake_run):
        paths = reader.extract_frames(max_frames=3)
    assert len(paths) == 3


# ---------------------------------------------------------------------------
# _adapt_frame_budget
# ---------------------------------------------------------------------------

def _gen():
    g = NoteGenerator.__new__(NoteGenerator)
    g._update_status = lambda *a, **k: None
    return g


def _probe(monkeypatch, duration):
    # 注意：test_video_reader_dedupe 在收集期会把 sys.modules["ffmpeg"] 换成假模块
    # （直接赋值，不是 setdefault），所以必须对「运行时实际注册的模块对象」打补丁，
    # 打在测试文件顶部 import 的对象上会扑空。
    import sys
    monkeypatch.setattr(
        sys.modules["ffmpeg"], "probe",
        lambda *_a, **_k: {"format": {"duration": str(duration)}},
    )


def _budgets(monkeypatch, frame=2400, image=500):
    monkeypatch.setattr(note_mod, "FRAME_BUDGET", frame)
    monkeypatch.setattr(note_mod, "IMAGE_BUDGET", image)


def test_short_video_unchanged(tmp_path, monkeypatch):
    _budgets(monkeypatch)
    _probe(monkeypatch, 600)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (6, [2, 2])


def test_4h_video_stretches_interval_keeps_2x2(tmp_path, monkeypatch):
    """4 小时视频：拼图 600>500 超预算 → 间隔 6→8s（≤2 倍设定值，不升格子）。"""
    _budgets(monkeypatch)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (8, [2, 2])


def test_10h_video_expands_grid_when_it_buys_density(tmp_path, monkeypatch):
    """10 小时视频：2×2 要拉到 18s（>2 倍设定值）；升 3×3 后帧预算把间隔压到
    15s——升格换回密度，按设计升 3×3（2400 帧 / 267 张拼图）。"""
    _budgets(monkeypatch)
    _probe(monkeypatch, 36000)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (15, [3, 3])


def test_grid_expands_only_when_it_buys_density(tmp_path, monkeypatch):
    """旧拼图预算（150）下 2×2 要拉到 24s（>2 倍），升 3×3 能换回 11s → 升格。"""
    _budgets(monkeypatch, frame=2400, image=150)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (11, [3, 3])


def test_frame_budget_binding_keeps_grid(tmp_path, monkeypatch):
    """帧预算（600）是硬约束时，升格子换不回帧数 → 保 2×2 直接拉间隔。"""
    _budgets(monkeypatch, frame=600, image=150)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (24, [2, 2])

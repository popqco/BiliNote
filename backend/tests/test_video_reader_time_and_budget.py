r"""video_reader 时间戳解析（100+ 分钟视频）与 _adapt_frame_budget 预算阶梯单测。

背景：
- 旧帧名正则 \d{2} 只认两位分钟，≥100 分钟视频排序 key 全变 inf（顺序乱）、
  拼图上时间戳消失；
- 预算阶梯（2026-10-11 用户定版：逐级升格优先）——兜不住先升 2×2、再升 3×3，
  每级都用原间隔试；实在兜不住才拉采样间隔。
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

    def fake_run(cmd, check=True, **kwargs):
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
    # 2026-10-11 起预算走 TaskConfigManager 运行时读取，测试直接钉住取值函数
    monkeypatch.setattr(note_mod, "_frame_budget", lambda: frame)
    monkeypatch.setattr(note_mod, "_image_budget", lambda: image)


def test_short_video_unchanged(tmp_path, monkeypatch):
    _budgets(monkeypatch)
    _probe(monkeypatch, 600)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (6, [2, 2])


def test_mild_overrun_upgrades_2x2_first(tmp_path, monkeypatch):
    """逐级升格（用户定版）：1×1 拼图 600>500 轻微超预算 → 升 2×2、间隔不动。"""
    _budgets(monkeypatch)
    _probe(monkeypatch, 3600)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [1, 1]) == (6, [2, 2])


def test_4h_video_upgrades_to_3x3_keeps_interval(tmp_path, monkeypatch):
    """4 小时视频：2×2 拼图 600>500 超预算 → 升 3×3 后 267 张兜得住 → 间隔保持 6s。"""
    _budgets(monkeypatch)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (6, [3, 3])


def test_2x2_over_3x3_fits_adopts_3x3(tmp_path, monkeypatch):
    """1×1 → 2×2 仍超（200>150）→ 3×3 兜住（89≤150）→ 升 3×3、间隔不动。"""
    _budgets(monkeypatch, frame=2400, image=150)
    _probe(monkeypatch, 4800)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [1, 1]) == (6, [3, 3])


def test_10h_video_stretches_interval_at_3x3(tmp_path, monkeypatch):
    """10 小时视频：3×3 也要 666>500 兜不住 → 升满 3×3 后才拉间隔到 15s
    （帧预算硬约束：36000/2400）。"""
    _budgets(monkeypatch)
    _probe(monkeypatch, 36000)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (15, [3, 3])


def test_grid_expands_before_interval_touches(tmp_path, monkeypatch):
    """旧拼图预算（150）下 4h 视频：2×2 超（600>150）、3×3 兜不住（267>150）
    → 升满 3×3 再拉间隔 6→11s。"""
    _budgets(monkeypatch, frame=2400, image=150)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (11, [3, 3])


def test_frame_budget_binding_upgrades_grid_then_stretches(tmp_path, monkeypatch):
    """帧预算（600）是硬约束时格子换不回帧数，但阶梯仍先升满 3×3（拼图数
    267>150 也超）再拉间隔到 24s——与旧策略的区别是格子跟着升满。"""
    _budgets(monkeypatch, frame=600, image=150)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [2, 2]) == (24, [3, 3])


def test_configured_3x3_goes_straight_to_interval(tmp_path, monkeypatch):
    """配置已是 3×3：没有升格空间 → 直接拉间隔（267>150 → 11s）。"""
    _budgets(monkeypatch, frame=2400, image=150)
    _probe(monkeypatch, 14400)
    assert _gen()._adapt_frame_budget(tmp_path / "v.mp4", 6, [3, 3]) == (11, [3, 3])

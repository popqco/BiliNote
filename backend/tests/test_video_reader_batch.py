"""整段抽帧（VideoReader._extract_frames_batch）与相邻去重单测。

背景（2026-10-28 实测）：逐帧 `ffmpeg -ss` 在 8 个任务并发下退化成 0.9 帧/秒，
因为 882 帧要 spawn 882 个进程；整段 `fps=1/N` 解码一遍只要 22 秒。批处理路径
必须保证：输出命名仍是 frame_MM_SS.jpg（下游拼图角标/截图编排/降级复用都按
这个名字解析时间戳）、帧数对得上、失败时能回退。
"""
import os

import pytest

from app.utils import video_reader as vr_mod
from app.utils.video_reader import VideoReader


def _reader(tmp_path, interval=1, **kwargs):
    return VideoReader(
        video_path="dummy.mp4",
        frame_dir=str(tmp_path / "frames"),
        grid_dir=str(tmp_path / "grids"),
        frame_interval=interval,
        **kwargs,
    )


class _FakeProc:
    """假 ffmpeg 进程：构造时按输出 pattern 写出 n 帧，poll 立刻返回 rc。"""

    def __init__(self, cmd, n_frames=5, rc=0, **_kwargs):
        self.returncode = rc
        if rc == 0:
            pattern = cmd[-1]
            for i in range(1, n_frames + 1):
                with open(pattern % i, "wb") as f:
                    f.write(b"jpeg-%d" % i)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def test_batch_writes_renamed_frame_MM_SS_files(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "frames", exist_ok=True)
    reader = _reader(tmp_path, interval=1)
    monkeypatch.setattr(
        vr_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(cmd, n_frames=5),
    )

    paths = reader._extract_frames_batch([0, 1, 2, 3, 4])

    names = [os.path.basename(p) for p in paths]
    assert names == ["frame_00_00.jpg", "frame_00_01.jpg", "frame_00_02.jpg",
                     "frame_00_03.jpg", "frame_00_04.jpg"]
    # 时间戳解析仍然成立（拼图角标/截图编排依赖它）
    assert [reader.extract_time_from_filename(n) for n in names] == [0, 1, 2, 3, 4]
    # 中间产物不残留
    assert not any(n.startswith("frame_0000") for n in os.listdir(tmp_path / "frames"))


def test_batch_interval_scales_timestamps(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "frames", exist_ok=True)
    reader = _reader(tmp_path, interval=3)
    monkeypatch.setattr(
        vr_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(cmd, n_frames=3),
    )

    paths = reader._extract_frames_batch([0, 3, 6])
    names = [os.path.basename(p) for p in paths]
    assert names == ["frame_00_00.jpg", "frame_00_03.jpg", "frame_00_06.jpg"]


def test_batch_nonzero_returncode_raises(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "frames", exist_ok=True)
    reader = _reader(tmp_path)
    monkeypatch.setattr(
        vr_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(cmd, rc=1),
    )
    with pytest.raises(RuntimeError):
        reader._extract_frames_batch([0, 1])


def test_batch_no_output_raises(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "frames", exist_ok=True)
    reader = _reader(tmp_path)
    monkeypatch.setattr(
        vr_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(cmd, n_frames=0),
    )
    with pytest.raises(RuntimeError):
        reader._extract_frames_batch([0, 1])


def test_should_use_batch_thresholds(tmp_path, monkeypatch):
    reader = _reader(tmp_path, interval=1)
    assert reader._should_use_batch(100) is True
    # 稀疏采样：整段解码要按视频时长全解一遍，不如逐帧 seek
    sparse = _reader(tmp_path, interval=10)
    assert sparse._should_use_batch(100) is False
    # 帧数太少时批处理不划算
    assert reader._should_use_batch(10) is False

    monkeypatch.setenv("FRAME_EXTRACT_LEGACY", "1")
    assert reader._should_use_batch(100) is False


def test_dedupe_drops_only_identical_adjacent_frames(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    same_a = frames / "frame_00_00.jpg"
    same_b = frames / "frame_00_01.jpg"
    other = frames / "frame_00_02.jpg"
    same_a.write_bytes(b"aaaa")
    same_b.write_bytes(b"aaaa")
    other.write_bytes(b"bbbbbb")

    reader = _reader(tmp_path)
    kept = reader._dedupe_adjacent([str(same_a), str(same_b), str(other)])

    assert [os.path.basename(p) for p in kept] == ["frame_00_00.jpg", "frame_00_02.jpg"]
    assert not same_b.exists()

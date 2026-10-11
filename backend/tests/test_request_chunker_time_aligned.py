"""chunk_time_aligned（时间对齐分批喂图）单测。

背景（2026-10-11）：旧 chunk() 按字节切文本，45MB 预算下转写几乎永远整个装进
第 1 块，超出的拼图全部溢出成 segments=[] 的无文本纯图块。chunk_time_aligned
把切分依据换成批次数：转写按时间轴切 N 窗、拼图按时间占比摊进各窗。
"""
from dataclasses import dataclass

import pytest

from app.gpt.request_chunker import RequestChunker


@dataclass
class DummySeg:
    start: float
    end: float
    text: str


def _seg_text(s):
    return s.get("text", "") if isinstance(s, dict) else s.text


def build_messages(segments, image_urls, **_):
    content = [{"type": "text", "text": "".join(_seg_text(s) for s in segments)}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
    return [{"role": "user", "content": content}]


def size_estimator(messages):
    size = 0
    for part in messages[0]["content"]:
        if part["type"] == "text":
            size += len(part["text"])
        else:
            size += len(part["image_url"]["url"])
    return size


def _chunker(max_bytes=10 ** 9, per_chunk=20):
    return RequestChunker(
        build_messages, max_bytes=max_bytes, size_estimator=size_estimator,
        max_images_per_chunk=per_chunk,
    )


def test_no_images_returns_none():
    segments = [DummySeg(0, 1, "a" * 100), DummySeg(1, 2, "b" * 100)]
    assert _chunker().chunk_time_aligned(segments, []) is None


def test_images_within_cap_returns_none():
    segments = [DummySeg(0, 1, "a" * 100), DummySeg(1, 2, "b" * 100)]
    images = [f"img{i}" for i in range(20)]
    assert _chunker().chunk_time_aligned(segments, images) is None


def test_batches_are_time_aligned_and_capped():
    segments = [DummySeg(i * 10, (i + 1) * 10, f"seg{i:03d}" * 10) for i in range(60)]
    images = [f"img{i:02d}" for i in range(45)]
    chunks = _chunker(per_chunk=20).chunk_time_aligned(segments, images)
    assert chunks is not None
    assert len(chunks) == 3
    # 每批图数 ≤ 上限，且连续切片保序
    all_images = []
    for c in chunks:
        assert len(c.image_urls) <= 20
        all_images.extend(c.image_urls)
    assert all_images == images
    # 转写窗口时间连续、不重叠、覆盖全部段
    for prev, cur in zip(chunks, chunks[1:]):
        assert prev.segments[-1].end <= cur.segments[0].start
    assert [s for c in chunks for s in c.segments] == segments
    # meta：批次序号 / 总数 / 时间范围
    assert [c.meta["batch_index"] for c in chunks] == [1, 2, 3]
    assert all(c.meta["batch_total"] == 3 for c in chunks)
    assert chunks[0].meta["time_start"] == 0
    assert chunks[0].meta["time_end"] == chunks[0].segments[-1].end


def test_fewer_segments_than_batches_returns_none():
    segments = [DummySeg(0, 1, "a" * 10), DummySeg(1, 2, "b" * 10)]
    images = [f"img{i}" for i in range(45)]
    # 只能开出 2 个窗，摊不下 45 张图（每窗 23 > 20）→ 回退旧逻辑
    assert _chunker(per_chunk=20).chunk_time_aligned(segments, images) is None


def test_oversized_batch_returns_none():
    segments = [DummySeg(i, i + 1, "x" * 500) for i in range(30)]
    images = [f"img{i}" for i in range(45)]
    # 每窗 500 字节文本 + 15 张图远超 max_bytes=100 → 回退
    assert _chunker(max_bytes=100, per_chunk=20).chunk_time_aligned(segments, images) is None


def test_dict_segments_supported():
    segments = [{"start": i * 5.0, "end": (i + 1) * 5.0, "text": f"d{i}" * 20} for i in range(40)]
    images = [f"img{i}" for i in range(40)]
    chunks = _chunker(per_chunk=20).chunk_time_aligned(segments, images)
    assert chunks is not None
    assert len(chunks) == 2
    assert chunks[0].meta["time_start"] == 0
    assert chunks[-1].meta["time_end"] == 200
    assert [s for c in chunks for s in c.segments] == segments


def test_legacy_chunk_still_works():
    """旧路径不受影响：按字节切文本 + 图片顺延。"""
    segments = [DummySeg(0, 1, "aaaa"), DummySeg(1, 2, "bbbb"), DummySeg(2, 3, "cccc")]
    chunker = _chunker(max_bytes=8, per_chunk=20)
    chunks = chunker.chunk(segments, [])
    texts = ["".join(seg.text for seg in c.segments) for c in chunks]
    assert "".join(texts) == "aaaabbbbcccc"

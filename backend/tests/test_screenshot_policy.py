"""原片截图密度策略单测（2026-10-27）。

覆盖用户实拍问题：单篇 790 张、同一画面连截、每句话后都插图。
策略必须在「模型不配合」时也能兜底，所以这里全部按纯函数测试。
"""
import pytest

# test_video_reader_dedupe 在收集期会把 sys.modules["PIL"] 永久换成假包；
# 这里在自己模块导入时（字母序更早）抓住真 PIL，避免全套跑时 dHash 测试被污染。
try:
    from PIL import Image as _RealPILImage
    from PIL import ImageDraw as _RealPILImageDraw
except Exception:  # pragma: no cover - 无 PIL 的环境直接跳过
    _RealPILImage = None
    _RealPILImageDraw = None

from app.utils.screenshot_marker import (
    iter_screenshot_markers,
    extract_screenshot_timestamps,
    strip_screenshot_markers,
)
from app.utils.screenshot_policy import (
    DEFAULT_MAX_COUNT,
    NearDuplicateFilter,
    dhash,
    effective_limits,
    hamming,
    iter_image_refs,
    resolve_interval,
    select_image_refs,
    select_markers,
)


def _marker_block(start_min: int, count: int, step_sec: int = 60) -> str:
    """生成 count 个连续时间戳标记，返回 markdown 片段。"""
    out = []
    for i in range(count):
        seconds = start_min * 60 + i * step_sec
        out.append(f"*Screenshot-[{seconds // 60:02d}:{seconds % 60:02d}]*")
        out.append(f"图注 {i}")
    return "\n".join(out) + "\n"


def test_iter_markers_returns_positions():
    md = "前文\n*Screenshot-[01:23]* 图注\n后文 Screenshot-02:03 收尾\n"
    markers = iter_screenshot_markers(md)
    assert [(m.ts, m.marker) for m in markers] == [
        (83, "*Screenshot-[01:23]*"),
        (123, "Screenshot-02:03"),
    ]
    # 位置必须能精确置换回原文
    assert md[markers[0].start : markers[0].end] == "*Screenshot-[01:23]*"
    assert md[markers[1].start : markers[1].end] == "Screenshot-02:03"
    # 旧 API 保持兼容
    assert extract_screenshot_timestamps(md) == [
        ("*Screenshot-[01:23]*", 83),
        ("Screenshot-02:03", 123),
    ]
    assert strip_screenshot_markers(md) == "前文\n 图注\n后文  收尾\n"


def test_resolve_interval_presets():
    assert resolve_interval("sparse") == 180
    assert resolve_interval("normal") == 90
    assert resolve_interval("rich") == 45
    assert resolve_interval(None, fallback=120) == 120
    assert resolve_interval("unknown", fallback=0) >= 1


def test_effective_limits_follow_duration_for_short_video():
    cap, min_gap, interval = effective_limits(600, 90, 60)
    assert cap == 7  # 10 分钟 / 90s，不能因为上限 60 就硬凑
    assert interval == 90
    assert min_gap == 15

    cap_long, _, _ = effective_limits(3600 * 3, 90, 60)
    assert cap_long == 60  # 长视频仍受配置上限约束

    cap_unknown, _, _ = effective_limits(None, 45, 60)
    assert cap_unknown == 60


def test_duration_cap_reduces_marker_count():
    md = "## 一\n" + _marker_block(0, 60, step_sec=60)  # 60 张，跨度 59 分钟
    markers = iter_screenshot_markers(md)
    result = select_markers(md, markers, duration=600, interval=90, max_count=60)
    assert result.kept <= 7
    assert result.kept >= 1


def test_duplicate_timestamp_keeps_first_only():
    md = (
        "## 一\n"
        "*Screenshot-[01:00]* 第一处\n"
        "*Screenshot-[01:00]* 第二处\n"
        "*Screenshot-[02:00]* 第三处\n"
    )
    result = select_markers(
        md, iter_screenshot_markers(md), duration=3600, interval=10, max_count=50
    )
    assert [m.ts for m in result.keep] == [60, 120]
    assert result.dropped.get("duplicate_ts") == 1


def test_out_of_range_timestamp_dropped():
    md = "## 一\n*Screenshot-[59:00]* 超时长\n*Screenshot-[01:00]* 正常\n"
    result = select_markers(
        md, iter_screenshot_markers(md), duration=600, interval=10, max_count=50
    )
    assert [m.ts for m in result.keep] == [60]
    assert result.dropped.get("out_of_range") == 1


def test_min_gap_kills_adjacent_bursts():
    md = (
        "## 一\n"
        "*Screenshot-[10:00]* a\n"
        "*Screenshot-[10:05]* b\n"
        "*Screenshot-[10:10]* c\n"
        "*Screenshot-[10:40]* d\n"
    )
    result = select_markers(
        md, iter_screenshot_markers(md), duration=3600, interval=90, max_count=60
    )
    # 15 秒最小间隔：10:00 保留，10:05/10:10 丢弃，10:40 保留
    assert [m.ts for m in result.keep] == [600, 640]
    assert result.dropped.get("too_close") == 2


def test_section_quota_prevents_one_section_from_eating_budget():
    # 第一节 3000 字 / 40 张，第二节 9000 字 / 4 张；全篇上限 10
    big = "## 大节\n" + ("正文" * 1500) + "\n" + _marker_block(0, 40, step_sec=60)
    small_markers = []
    for i in range(4):
        seconds = 40 * 60 + i * 60
        small_markers.append(f"*Screenshot-[{seconds // 60:02d}:{seconds % 60:02d}]*")
    small = "## 小节\n" + ("补充" * 4500) + "\n" + "\n".join(small_markers) + "\n"
    md = big + small

    result = select_markers(
        md, iter_screenshot_markers(md), duration=3600, interval=90, max_count=10
    )
    assert result.kept <= 10
    ts = [m.ts for m in result.keep]
    assert max(ts) >= 40 * 60  # 小节没有被大节挤掉
    assert len([t for t in ts if t < 40 * 60]) <= 9


def test_global_cap_keeps_first_and_last():
    md = "## 一\n" + _marker_block(0, 80, step_sec=60)
    result = select_markers(
        md, iter_screenshot_markers(md), duration=80 * 60, interval=90, max_count=12
    )
    markers = iter_screenshot_markers(md)
    assert result.kept <= 12
    assert result.keep[0].ts == markers[0].ts
    assert result.keep[-1].ts == markers[-1].ts


def test_keep_preserves_document_order():
    # 文档顺序与时间戳顺序不一致时，结果仍按文档位置返回
    md = (
        "## 一\n"
        "*Screenshot-[30:00]* 后\n"
        "*Screenshot-[10:00]* 前\n"
        "*Screenshot-[20:00]* 中\n"
    )
    result = select_markers(
        md, iter_screenshot_markers(md), duration=3600, interval=10, max_count=50
    )
    starts = [m.start for m in result.keep]
    assert starts == sorted(starts)
    assert [m.ts for m in result.keep] == [1800, 600, 1200]


def test_no_markers_is_noop():
    result = select_markers("## 一\n没有标记\n", [], duration=600)
    assert result.kept == 0
    assert result.dropped == {}


def test_near_duplicate_filter_window():
    flow = NearDuplicateFilter(window_sec=30, threshold=4)
    assert flow.is_duplicate(100, 0b1010) is False
    assert flow.is_duplicate(105, 0b1011) is True   # 5 秒内、距离 1
    assert flow.is_duplicate(200, 0b1010) is False  # 同画面但超出时间窗，保留
    assert flow.is_duplicate(210, 0b11111111) is False  # 距离太大，保留


def test_dhash_identical_and_different_images(tmp_path):
    if _RealPILImage is None or _RealPILImageDraw is None or not hasattr(_RealPILImage, "open"):
        pytest.skip("PIL 不可用")
    Image, ImageDraw = _RealPILImage, _RealPILImageDraw

    def save(name, color, box=None):
        img = Image.new("RGB", (160, 90), color)
        if box:
            ImageDraw.Draw(img).rectangle(box, fill=(255, 255, 255))
        path = tmp_path / name
        img.save(path, "JPEG", quality=90)
        return path

    a = save("a.jpg", (20, 20, 20), (10, 10, 80, 60))
    b = save("b.jpg", (20, 20, 20), (10, 10, 80, 60))
    c = save("c.jpg", (240, 240, 240), (90, 20, 150, 70))

    ha, hb, hc = dhash(a), dhash(b), dhash(c)
    assert ha is not None and hb is not None and hc is not None
    assert hamming(ha, hb) <= 2
    assert hamming(ha, hc) > 4
    assert dhash(tmp_path / "missing.jpg") is None


def test_default_max_count_is_sane():
    assert DEFAULT_MAX_COUNT == 60


def test_iter_image_refs_and_select_for_existing_notes():
    images = "\n".join(
        f"![](/static/screenshots/screenshot_{i:03}_x.jpg)" for i in range(40)
    )
    md = "## 大节\n" + images + "\n## 小节\n![](/static/screenshots/screenshot_099_x.jpg)\n"
    refs = iter_image_refs(md)
    assert len(refs) == 41
    assert refs[0].url == "/static/screenshots/screenshot_000_x.jpg"
    assert md[refs[0].start : refs[0].end] == "![](/static/screenshots/screenshot_000_x.jpg)"

    kept = select_image_refs(md, refs, cap=12)
    assert len(kept) <= 12
    # 小节各留一张，最后的 099 不会被大节挤掉
    assert kept[-1].url.endswith("screenshot_099_x.jpg")
    assert [r.start for r in kept] == sorted(r.start for r in kept)

    # 低于上限时原样保留
    assert select_image_refs(md, refs[:5], cap=12) == refs[:5]

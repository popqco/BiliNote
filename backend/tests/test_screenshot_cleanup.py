"""截图标记/图片删除后的残渣清理（2026-10-28）。

覆盖 review 发现的三个真实问题：
- 丢标记只删 marker，prompt 写的「图注：…」留成无图孤儿行；
- 无视频降级路径 strip 标记后同样留图注；
- 存量瘦身删图后留下 `- ：说明` / `*：说明` / 空 bullet。
"""
from pathlib import Path

from app.services import note as note_mod
from app.utils.screenshot_cleanup import (
    cleanup_residue,
    has_residue,
    remove_image_refs,
    strip_markers_with_captions,
)
from app.utils.screenshot_policy import (
    iter_image_refs,
    resolve_interval,
    select_image_refs,
)


def _generator(monkeypatch, tmp_path):
    def fake_shot(video, outdir, ts, idx):
        return str(tmp_path / f"fake_{idx}.jpg")

    monkeypatch.setattr(note_mod, "generate_screenshot", fake_shot)
    monkeypatch.setattr(note_mod, "dhash", lambda path: None)
    gen = note_mod.NoteGenerator.__new__(note_mod.NoteGenerator)
    gen.screenshot_interval = 90
    gen.screenshot_max_count = 60
    return gen


def test_dropped_markers_do_not_leave_orphan_captions(monkeypatch, tmp_path):
    gen = _generator(monkeypatch, tmp_path)
    markdown = (
        "## 第一节\n\n"
        "*Screenshot-[00:10]*\n图注：第一张图\n\n"
        "*Screenshot-[00:11]*\n图注：第二张图（太近被丢）\n\n"
        "*Screenshot-[05:00]*\n图注：第三张图\n\n"
        "## 第二节\n\n"
        "*Screenshot-[05:00]*\n图注：重复时间戳（被丢）\n"
    )

    out = gen._insert_screenshots(markdown, Path("dummy.mp4"), duration=600)

    assert out.count("![") == 2
    # 保留的两张图各带一条图注；被丢的两条图注不能留下
    assert out.count("图注") == 2
    assert "图注：第一张图" in out
    assert "图注：第三张图" in out
    assert "第二张图" not in out
    assert "重复时间戳" not in out
    assert "Screenshot-" not in out
    assert "\n\n\n" not in out


def test_failed_screenshot_removes_its_caption(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(note_mod, "generate_screenshot", boom)
    gen = note_mod.NoteGenerator.__new__(note_mod.NoteGenerator)
    gen.screenshot_interval = 90
    gen.screenshot_max_count = 60
    markdown = "## 一节\n\n*Screenshot-[00:10]*\n图注：失败画面\n\n正文保留\n"

    out = gen._insert_screenshots(markdown, Path("dummy.mp4"), duration=600)

    assert "图注" not in out
    assert "Screenshot-" not in out
    assert "正文保留" in out


def test_shared_caption_line_is_removed_once(monkeypatch, tmp_path):
    """同一行两个标记共享一行图注：删除区间必须先去重，否则会截断正文。"""
    gen = _generator(monkeypatch, tmp_path)
    markdown = (
        "## 一节\n"
        "*Screenshot-[00:10]* *Screenshot-[00:20]*\n"
        "图注：共享说明\n"
        "正文保留\n"
    )

    out = gen._insert_screenshots(markdown, Path("dummy.mp4"), duration=5)

    assert "图注" not in out
    assert "Screenshot-" not in out
    assert "正文保留" in out


def test_strip_markers_with_captions_removes_both():
    markdown = "## 一节\n*Screenshot-[00:10]*\n图注：说明\n正文\n"
    out = strip_markers_with_captions(markdown)
    assert "Screenshot-" not in out
    assert "图注" not in out
    assert "正文" in out


def test_strip_markers_preserves_inline_prose():
    markdown = "前文 Screenshot-00:10 后文\n图注：说明\n"
    out = strip_markers_with_captions(markdown)
    assert "前文" in out
    assert "后文" in out
    assert "Screenshot-" not in out
    assert "图注" not in out


def test_cleanup_ignores_fenced_code_blocks():
    markdown = "```text\n- \n*：代码里的星号\n```\n正文\n"
    assert has_residue(markdown) is False
    assert cleanup_residue(markdown) == markdown


def test_strip_markers_handles_crlf_caption():
    markdown = "*Screenshot-[00:10]*\r\n图注：说明\r\n正文\r\n"
    out = strip_markers_with_captions(markdown)
    assert "Screenshot-" not in out
    assert "图注" not in out
    assert "正文" in out


def test_remove_image_refs_cleans_bullets_and_colons():
    markdown = (
        "- ![](/static/screenshots/a.jpg)：说明一\n"
        "![](/static/screenshots/b.jpg)\n"
        "* ![](/static/screenshots/c.jpg)：说明二\n"
        "正文 ![](/static/screenshots/d.jpg) 保留\n"
    )
    refs = iter_image_refs(markdown)
    out = remove_image_refs(markdown, [(r.start, r.end) for r in refs])

    assert "说明一" in out
    assert "说明二" in out
    assert "正文" in out and "保留" in out
    assert "- 说明一" in out
    assert "* 说明二" in out
    assert "*：说明二" not in out
    assert "\n- \n" not in out
    assert "/static/screenshots/" not in out


def test_remove_image_refs_drops_following_caption():
    markdown = (
        "![0:10](/static/screenshots/a.jpg)\n"
        "图注：第一张\n"
        "正文\n"
    )
    refs = iter_image_refs(markdown)
    out = remove_image_refs(markdown, [(r.start, r.end) for r in refs])
    assert "图注" not in out
    assert "正文" in out


def test_remove_image_refs_drops_empty_numbered_item():
    markdown = "1. ![](/static/screenshots/a.jpg)\n正文\n"
    refs = iter_image_refs(markdown)
    out = remove_image_refs(markdown, [(r.start, r.end) for r in refs])
    assert "1." not in out
    assert "正文" in out


def test_remove_image_refs_drops_inline_caption():
    markdown = "![0:10](/static/screenshots/a.jpg) 图注：同一行\n正文\n"
    refs = iter_image_refs(markdown)
    out = remove_image_refs(markdown, [(r.start, r.end) for r in refs])
    assert "图注" not in out
    assert "正文" in out


def test_cleanup_preserves_markdown_hard_breaks():
    markdown = "行一  \n行二\n- ：说明\n"
    out = cleanup_residue(markdown)
    assert "行一  \n行二" in out
    assert "- 说明" in out


def test_cleanup_ignores_indented_code_blocks():
    markdown = "    - \n    *：代码\n正文\n"
    assert has_residue(markdown) is False
    assert cleanup_residue(markdown) == markdown


def test_mixed_kept_and_dropped_markers_keep_shared_caption(monkeypatch, tmp_path):
    gen = _generator(monkeypatch, tmp_path)
    markdown = (
        "## 一节\n"
        "*Screenshot-[00:20]* *Screenshot-[00:10]*\n"
        "图注：共享说明\n"
        "正文\n"
    )
    out = gen._insert_screenshots(markdown, Path("dummy.mp4"), duration=600)
    assert out.count("![") == 1
    # 00:10 保留、00:20 因间隔太近丢弃；共享图注属于活下来的那张图
    assert "图注：共享说明" in out
    assert "Screenshot-" not in out


def test_gallery_section_is_capped_to_one_shot():
    text = "正文内容句子。" * 1000
    images = "\n".join(
        f"![](/static/screenshots/screenshot_{i:03}_x.jpg)" for i in range(20)
    )
    markdown = "## 正文\n" + text + "\n## 图库\n" + images + "\n"
    refs = iter_image_refs(markdown)
    kept = select_image_refs(markdown, refs, cap=20)
    # 纯图小节没有可计权重，不应靠图片数量撑大配额去吞掉全篇预算
    assert len(kept) == 1


def test_has_residue_only_flags_real_artifacts():
    assert has_residue("- ：说明\n") is True
    assert has_residue("*：说明\n") is True
    assert has_residue("- \n") is True
    assert has_residue("正常列表\n- 正常条目\n") is False
    # 普通空行 / 行尾空格不算残渣，避免维护时重写整库
    assert has_residue("正文  \n\n") is False
    assert cleanup_residue("- ：说明\n") == "- 说明\n"


def test_section_quota_not_inflated_by_image_syntax():
    """等长的两个小节：图片多的小节不能因为图片语法本身变长而多拿配额。"""
    text = "正文内容句子。" * 1000
    a = "## 甲\n" + text + "\n" + "\n".join(
        f"![](/static/screenshots/screenshot_{i:03}_x.jpg)" for i in range(200)
    ) + "\n"
    b = (
        "## 乙\n"
        + text
        + "\n![](/static/screenshots/screenshot_z1.jpg)\n"
        + "![](/static/screenshots/screenshot_z2.jpg)\n"
    )
    refs = iter_image_refs(a + b)
    kept = select_image_refs(a + b, refs, cap=40)
    kept_a = sum(1 for ref in kept if "screenshot_z" not in ref.url)
    kept_b = sum(1 for ref in kept if "screenshot_z" in ref.url)

    assert kept_b == 2
    assert kept_a <= 21


def test_resolve_interval_global_uses_fallback():
    assert resolve_interval("global", 120) == 120
    assert resolve_interval(None, 120) == 120
    assert resolve_interval("sparse", 120) == 180

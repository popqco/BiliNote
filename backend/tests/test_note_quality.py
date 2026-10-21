import pytest

from app.utils.note_quality import NoteContentError, ensure_note_content


REFUSAL_NOTE = """\
> 来源链接：https://www.youtube.com/watch?v=GL6ClBBPMxU

抱歉，这个问题未找到相关结果。

## AI 总结

笔记中没有可提取的实质内容，仅有一句“未找到相关结果”的提示，无法据此概括主题、作者观点或给出任何关键信息。
"""


def test_refusal_with_ai_summary_is_rejected_as_fatal():
    with pytest.raises(NoteContentError) as exc:
        ensure_note_content(REFUSAL_NOTE, "很长的转写" * 2000)
    assert exc.value.fatal is True
    assert "拒答" in str(exc.value)


def test_empty_content_is_rejected():
    with pytest.raises(NoteContentError) as exc:
        ensure_note_content("   \n", "")
    assert exc.value.fatal is False


def test_toc_and_summary_only_is_rejected():
    md = """\
## 目录

- 第一段（00:00）
- 第二段（01:00）

## AI 总结

本篇只有目录和总结，没有正文。
"""
    with pytest.raises(NoteContentError) as exc:
        ensure_note_content(md, "转写内容" * 500)
    assert exc.value.fatal is False


def test_valid_long_note_passes():
    body = "\n\n".join(
        f"## 第{i}部分\n\n这是对视频内容的实质性总结，包含观点、事实与结论。" * 3
        for i in range(1, 6)
    )
    ensure_note_content(f"> 来源链接：https://example.com\n\n{body}\n\n## AI 总结\n\n简短总结", "转写" * 5000)


def test_h1_toc_does_not_swallow_h2_body():
    # 真实笔记常见 `# 目录` 后直接接 `##` 正文：目录剥离不能按同级标题规则
    # 把整篇正文吞掉（2026-10-21 扫描实拍：5205 字正常笔记被判 0 字）。
    md = (
        "# 目录\n\n"
        "- [1. 引言](#1)\n"
        "- [2. 结论](#2)\n"
        "- [AI 总结](#ai)\n\n"
        "## 1. 引言\n\n"
        + "这部分是正常的视频正文内容，包含观点与事实。" * 30
    )
    ensure_note_content(md, "转写" * 5000)


def test_short_transcript_allows_short_note():
    md = "## 要点\n\n" + "这是一段短视频的有效内容。" * 5
    ensure_note_content(md, "短转写" * 20)


def test_english_refusal_is_rejected():
    with pytest.raises(NoteContentError) as exc:
        ensure_note_content("I'm sorry, but I can't assist with that request.", "a" * 5000)
    assert exc.value.fatal is True


def test_refusal_phrase_inside_a_real_long_note_is_not_fatal():
    # 合法笔记里引用「未找到相关结果」不应误伤（正文远大于阈值）
    body = "## 搜索功能分析\n\n" + "视频讨论了搜索页为什么会出现未找到相关结果的情况。" * 40
    ensure_note_content(body, "转写" * 5000)

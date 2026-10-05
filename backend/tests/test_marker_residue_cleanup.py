"""_cleanup_marker_residue 的回归测试。

背景（2026-10-05）：模型把 *Screenshot-[mm:ss]* 写成斜体包裹形态，后端替换
标记时只消费前导 `*`，闭合的 `*` 残留在笔记里——图片行变成 `![](...)*`、
标题行变成 `[原片 @ mm:ss](url)*`。这些残留会在阅读视图里显示为杂散星号。
"""

from app.services.note import _cleanup_marker_residue


def test_image_line_trailing_asterisk_is_stripped():
    """香水笔记实测形态：独立图片行尾挂星号（规则3：图片链接行尾孤星）。"""
    md = "![](/static/screenshots/screenshot_000_e1595695.jpg)*\n"
    assert _cleanup_marker_residue(md) == "![](/static/screenshots/screenshot_000_e1595695.jpg)\n"


def test_image_line_double_asterisk_is_stripped():
    md = "![](/static/screenshots/screenshot_001.jpg)**\n"
    assert _cleanup_marker_residue(md) == "![](/static/screenshots/screenshot_001.jpg)\n"


def test_image_with_alt_and_space_asterisk_is_stripped():
    md = "![截图](/static/screenshots/screenshot_002.jpg) *\n"
    assert _cleanup_marker_residue(md) == "![截图](/static/screenshots/screenshot_002.jpg)\n"


def test_heading_with_video_link_trailing_asterisk_is_stripped():
    """香水笔记实测形态：标题行原片链接后挂星号（规则2 既有行为，防回归）。"""
    md = "## 核心结论 [原片 @ 00:00](https://www.bilibili.com/video/BV1?t=0)*\n"
    assert _cleanup_marker_residue(md) == "## 核心结论 [原片 @ 00:00](https://www.bilibili.com/video/BV1?t=0)\n"


def test_lone_asterisk_line_is_removed():
    """规则1 既有行为，防回归。"""
    assert _cleanup_marker_residue("前文\n*\n后文\n") == "前文\n后文\n"


def test_normal_bold_italic_lines_are_untouched():
    """正常强调语法不能被误伤。"""
    md = "- **重点**：这是加粗内容\n- *斜体说明* 另一段正常文字\n"
    assert _cleanup_marker_residue(md) == md


def test_normal_image_without_asterisk_is_untouched():
    md = "![封面](https://example.com/cover.jpg)\n"
    assert _cleanup_marker_residue(md) == md


def test_asterisk_after_link_midline_is_untouched():
    """行中间 `)*` 不是残留形态（后面还有正文），不摘。"""
    md = "[原片 @ 01:00](https://x.com/v?t=60)* 后续正文\n"
    assert _cleanup_marker_residue(md) == md

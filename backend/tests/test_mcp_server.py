"""MCP server 输出整形单元测试（不依赖后端、不启动 stdio）。

覆盖：qa/full 渲染、按节读取、截断、outline、检索过滤、时间格式化。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server as m  # noqa: E402


def _sample_data():
    md = """## 目录

- 1. 现象 (00:00)

## 现象：波形干净 (00:00)*

- 一块 PCB 的原理图检查正常。

![screenshot](http://example/a.jpg)

## 对策：别让回流影子断开 (02:46)*

- **高速信号不跨缝**。
- **必须跨缝时加缝合电容**：在走线旁边放一颗电容。

![screenshot](http://example/b.jpg)

## 案例验证与结论 (03:11)*

- 加电容后 RE 从 FAIL 变 PASS。

## AI 总结

- 这是合成总结。
"""
    return {
        "status": "SUCCESS",
        "result": {
            "markdown": md,
            "audio_meta": {
                "title": "测试笔记",
                "platform": "bilibili",
                "duration": 211,
                "raw_info": {
                    "uploader": "tester",
                    "webpage_url": "https://example/v",
                },
            },
        },
    }


class TestMcpFormatting(unittest.TestCase):
    def test_strip_images(self):
        out = m._strip_images("a\n\n![x](y.png)\n\nb")
        self.assertNotIn("![", out)
        self.assertNotIn("y.png", out)
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_strip_ai_summary(self):
        md = _sample_data()["result"]["markdown"]
        out = m._strip_ai_summary(md)
        self.assertNotIn("## AI 总结", out)
        self.assertIn("案例验证", out)

    def test_split_sections(self):
        md = _sample_data()["result"]["markdown"]
        titles = [t for t, _ in m._split_sections(md)]
        self.assertIn("对策：别让回流影子断开 (02:46)*", titles)

    def test_find_section_by_keyword_and_timestamp(self):
        md = _sample_data()["result"]["markdown"]
        found, _, matches = m._find_section(md, "对策")
        self.assertIsNotNone(found)
        self.assertEqual(matches, 1)
        self.assertIn("缝合电容", found[1])

        found2, _, _ = m._find_section(md, "02:46")
        self.assertIsNotNone(found2)
        self.assertEqual(found2[0], found[0])

    def test_render_note_qa_strips_noise(self):
        out = m._render_note(_sample_data(), "t1")
        self.assertNotIn("![", out)
        self.assertNotIn("## AI 总结", out)
        self.assertIn("对策", out)
        self.assertIn('format="full"', out)

    def test_render_note_full_keeps_images(self):
        out = m._render_note(_sample_data(), "t1", fmt="full")
        self.assertIn("![", out)
        self.assertIn("## AI 总结", out)

    def test_render_note_section_only(self):
        out = m._render_note(_sample_data(), "t1", section="对策")
        self.assertIn("对策", out)
        self.assertIn("缝合电容", out)
        self.assertNotIn("案例验证与结论", out)
        self.assertNotIn("![](", out)

    def test_render_note_missing_section_lists_titles(self):
        out = m._render_note(_sample_data(), "t1", section="不存在")
        self.assertIn("未找到匹配小节", out)
        self.assertIn("对策", out)

    def test_render_note_max_chars(self):
        out = m._render_note(_sample_data(), "t1", max_chars=300)
        self.assertLessEqual(len(out), 400)
        self.assertIn("已截断", out)

    def test_outline_skips_toc_and_marks_ai(self):
        out = m._render_outline(_sample_data(), "t1")
        self.assertNotIn("1. 目录", out)
        self.assertIn("[合成]", out)
        self.assertIn("对策", out)

    def test_search_filters(self):
        results = [
            {"source_type": "markdown", "section_title": "对策", "text": "a"},
            {"source_type": "markdown", "section_title": "AI 总结", "text": "b"},
            {"source_type": "markdown", "section_title": "目录", "text": "c"},
            {"source_type": "transcript", "section_title": "", "text": "d"},
        ]
        kept = [r for r in results if m._search_keep(r, "note", False, None)]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["section_title"], "对策")

        kept_t = [r for r in results if m._search_keep(r, "transcript", False, None)]
        self.assertEqual(len(kept_t), 1)
        self.assertEqual(kept_t[0]["source_type"], "transcript")

        kept_s = [r for r in results if m._search_keep(r, "note", True, "总结")]
        self.assertEqual(len(kept_s), 1)
        self.assertEqual(kept_s[0]["section_title"], "AI 总结")

    def test_fmt_datetime(self):
        self.assertRegex(m._fmt_datetime(1792974733), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(m._fmt_datetime("bad"), "bad")

    def test_status_text(self):
        out = m._status_text("t1", {"status": "PENDING", "message": "排队中"})
        self.assertIn("PENDING", out)
        self.assertIn("排队中", out)


if __name__ == "__main__":
    unittest.main()

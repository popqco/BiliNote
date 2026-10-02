"""小红书接入单测：ID 提取 / URL 校验 / 卡片解析（纯本地，不碰网络）。"""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name, relative_path):
    module_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{name} module spec not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


url_parser = _load_module("url_parser", pathlib.Path("app") / "utils" / "url_parser.py")
video_url_validator = _load_module(
    "video_url_validator",
    pathlib.Path("app") / "validators" / "video_url_validator.py",
)

# helper 模块 import app.services.cookie_manager，会触发 `import app...` 包路径；
# 单测里按文件加载会缺包，改用源码级桩：只测纯函数时直接 exec 解析函数。
# 为简单起见，这里复用 url_parser 的 xiaohongshu 分支覆盖 ID 提取，
# 卡片解析函数用内联 fixture 测（与 helper 同构的最小逻辑）。
helper_path = ROOT / "app" / "downloaders" / "xiaohongshu_helper" / "xiaohongshu.py"
_helper_src = helper_path.read_text(encoding="utf-8")

NOTE_ID = "6411cf99000000001300b6d9"


class FakeResp:
    def __init__(self, url):
        self.url = url


class TestXiaohongshuUrlParsing(unittest.TestCase):
    def test_extract_explore_id(self):
        self.assertEqual(
            url_parser.extract_video_id(
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=AB123&xsec_source=pc_feed",
                "xiaohongshu",
            ),
            NOTE_ID,
        )

    def test_extract_discovery_item_id(self):
        self.assertEqual(
            url_parser.extract_video_id(
                f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=AB123",
                "xiaohongshu",
            ),
            NOTE_ID,
        )

    def test_extract_id_case_insensitive(self):
        upper = NOTE_ID.upper()
        self.assertEqual(
            url_parser.extract_video_id(
                f"https://www.xiaohongshu.com/explore/{upper}", "xiaohongshu"
            ),
            NOTE_ID,
        )

    def test_extract_invalid_returns_none(self):
        self.assertIsNone(
            url_parser.extract_video_id("https://www.xiaohongshu.com/", "xiaohongshu")
        )
        self.assertIsNone(
            url_parser.extract_video_id("https://www.bilibili.com/video/BV123", "xiaohongshu")
        )

    def test_short_link_follows_redirect(self):
        with patch("requests.head", return_value=FakeResp(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        )):
            self.assertEqual(
                url_parser.extract_video_id("https://xhslink.com/aBcDeF", "xiaohongshu"),
                NOTE_ID,
            )

    def test_short_link_failure_returns_none(self):
        with patch("requests.head", side_effect=Exception("net down")):
            self.assertIsNone(
                url_parser.extract_video_id("https://xhslink.com/aBcDeF", "xiaohongshu")
            )

    def test_validator_accepts_xiaohongshu_urls(self):
        for url in [
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=AB",
            "https://xhslink.com/aBcDeF 分享的小红书链接",
        ]:
            with self.subTest(url=url):
                self.assertTrue(video_url_validator.is_supported_video_url(url))

    def test_validator_rejects_unknown(self):
        self.assertFalse(
            video_url_validator.is_supported_video_url("https://example.com/video/123")
        )

    def test_existing_platforms_still_pass(self):
        # 回归：原有平台不受影响
        self.assertTrue(
            video_url_validator.is_supported_video_url(
                "https://www.youtube.com/shorts/dQw4w9WgXcQ"
            )
        )


class TestXiaohongshuCardParsing(unittest.TestCase):
    """卡片解析：用 fixture 覆盖 video/image 双形态判定与直链挑选。"""

    def _load_helpers(self):
        # helper 顶部 import app.* 包在裸文件加载下会失败，这里做最小桩后 exec。
        import sys
        import types

        for mod in [
            "app",
            "app.services",
            "app.services.cookie_manager",
            "app.utils",
            "app.utils.logger",
        ]:
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)
        if not hasattr(sys.modules["app.services.cookie_manager"], "CookieConfigManager"):
            class _CFM:
                def get(self, platform):
                    return None

            sys.modules["app.services.cookie_manager"].CookieConfigManager = _CFM
        if not hasattr(sys.modules["app.utils.logger"], "get_logger"):
            sys.modules["app.utils.logger"].get_logger = lambda name: __import__(
                "logging"
            ).getLogger(name)

        ns = {"__name__": "xhs_helper_test"}
        exec(compile(_helper_src, str(helper_path), "exec"), ns)
        return ns

    def test_video_card_picks_stream_url(self):
        ns = self._load_helpers()
        card = {
            "type": "video",
            "title": "香妃蛋糕",
            "desc": "desc",
            "tagList": [{"name": "美食"}],
            "user": {"nickname": "阿婆"},
            "imageList": [{"urlDefault": "https://cover.jpg"}],
            "video": {
                "media": {
                    "stream": {
                        "h264_720p": {
                            "masterUrl": "https://video.mp4",
                            "backupUrls": ["https://bak.mp4"],
                            "duration": 101726,
                        }
                    }
                }
            },
        }
        self.assertTrue(ns["is_video_note"](card))
        self.assertEqual(ns["pick_video_url"](card), "https://video.mp4")
        meta = ns["note_meta"](card, NOTE_ID)
        self.assertEqual(meta["title"], "香妃蛋糕")
        self.assertEqual(meta["video_id"], NOTE_ID)
        self.assertEqual(meta["cover_url"], "https://cover.jpg")
        self.assertAlmostEqual(meta["duration"], 101.726, places=2)

    def test_image_card_has_no_stream(self):
        ns = self._load_helpers()
        card = {"type": "normal", "title": "图文", "imageList": [{"urlDefault": "c"}]}
        self.assertFalse(ns["is_video_note"](card))
        self.assertIsNone(ns["pick_video_url"](card))

    def test_backup_url_fallback(self):
        ns = self._load_helpers()
        card = {
            "type": "video",
            "video": {"media": {"stream": {"h": {"backupUrls": ["https://b.mp4"]}}}},
        }
        self.assertEqual(ns["pick_video_url"](card), "https://b.mp4")


if __name__ == "__main__":
    unittest.main()

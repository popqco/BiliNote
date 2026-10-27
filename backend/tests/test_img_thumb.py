"""原片截图缩略（/img_thumb）回归测试（2026-10-17 白屏根因修复配套）。

背景：笔记正文原片截图是 4K JPEG（解码单张约 31MB），重度笔记一篇引用可达
数百张（实测最多 790 张），WebView2 渲染进程整篇解码被 OOM 杀掉 → 页面永久
白屏。修复之一是把屏幕显示统一缩到 1600px 宽并落盘缓存。

覆盖点：
1. 大图缩到目标宽度并落盘缓存，二次调用命中缓存不再重算；
2. 原图本来就够小 → 直接发原文件、不生成缓存；
3. 相对路径与 127.0.0.1 绝对地址两种形态都要能解析；
4. 缺失文件 / 目录穿越 / 外链 → 404（不能变成任意文件读取）；
5. 宽度参数夹紧（240~4096）；
6. 坏图（不是有效图片）→ 回退原图，不能 500；
7. 非 JPEG 原图（png）媒体类型正确。
"""

import asyncio
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

import app.routers.note as note_router  # noqa: E402


def _make_image(path: pathlib.Path, width: int, height: int, fmt: str = "JPEG") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (120, 90, 200)).save(path, format=fmt)


class ImgThumbTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.shots = root / "screenshots"
        self.cache = root / "thumb_cache"
        self.shots.mkdir(parents=True, exist_ok=True)
        self._patches = [
            patch.object(note_router, "_SCREENSHOTS_DIR", self.shots),
            patch.object(note_router, "_THUMB_CACHE_DIR", self.cache),
            patch.object(note_router, "_thumb_cache_prune_tick", 0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def call(self, url, w=1600):
        return asyncio.run(note_router.img_thumb(url, w))

    def cached_files(self):
        if not self.cache.is_dir():
            return []
        return [p for p in self.cache.glob("*.jpg") if p.is_file()]


class ResizeAndCacheTests(ImgThumbTestBase):
    def test_big_image_is_resized_and_cached(self):
        _make_image(self.shots / "big.jpg", 3200, 1800)
        r = self.call("/static/screenshots/big.jpg", 1600)

        self.assertEqual(r.media_type, "image/jpeg")
        self.assertIn("immutable", str(r.headers))
        with Image.open(r.path) as im:
            self.assertEqual((im.width, im.height), (1600, 900))
        self.assertEqual(len(self.cached_files()), 1, "缩略应落盘到缓存目录")

    def test_second_call_hits_disk_cache(self):
        _make_image(self.shots / "big.jpg", 3200, 1800)
        r1 = self.call("/static/screenshots/big.jpg", 1600)
        r2 = self.call("/static/screenshots/big.jpg", 1600)

        self.assertEqual(r1.path, r2.path)
        self.assertEqual(len(self.cached_files()), 1, "第二次不得重算/新增缓存")

    def test_small_image_served_untouched_without_cache(self):
        _make_image(self.shots / "small.jpg", 800, 400)
        r = self.call("/static/screenshots/small.jpg", 1600)

        self.assertEqual(pathlib.Path(r.path), self.shots / "small.jpg")
        self.assertEqual(self.cached_files(), [], "够小的原图不生成缓存")

    def test_width_is_clamped(self):
        _make_image(self.shots / "huge.jpg", 5000, 1000)
        r = self.call("/static/screenshots/huge.jpg", 999999)
        with Image.open(r.path) as im:
            self.assertEqual(im.width, 4096)

        r2 = self.call("/static/screenshots/huge.jpg", 1)
        with Image.open(r2.path) as im:
            self.assertEqual(im.width, 240)

    def test_png_media_type_preserved(self):
        _make_image(self.shots / "shot.png", 400, 200, fmt="PNG")
        r = self.call("/static/screenshots/shot.png", 1600)
        self.assertEqual(r.media_type, "image/png")

    def test_corrupt_image_falls_back_to_original(self):
        (self.shots / "broken.jpg").write_bytes(b"this is not an image")
        r = self.call("/static/screenshots/broken.jpg", 1600)

        self.assertEqual(pathlib.Path(r.path), self.shots / "broken.jpg")
        self.assertEqual(self.cached_files(), [])


class UrlResolutionTests(ImgThumbTestBase):
    def test_relative_and_loopback_absolute_both_resolve(self):
        _make_image(self.shots / "a.jpg", 800, 400)
        r_rel = self.call("/static/screenshots/a.jpg", 1600)
        r_abs = self.call("http://127.0.0.1:8483/static/screenshots/a.jpg", 1600)
        r_local = self.call("http://localhost:8483/static/screenshots/a.jpg", 1600)
        # 远端手机经 Tailscale IP 打开时，MarkdownViewer 可能拼出 Worker 绝对地址
        r_tail = self.call("http://100.67.206.88:8483/static/screenshots/a.jpg", 1600)

        for r in (r_rel, r_abs, r_local, r_tail):
            self.assertEqual(pathlib.Path(r.path), self.shots / "a.jpg")

    def test_missing_file_is_404(self):
        r = self.call("/static/screenshots/nope.jpg", 1600)
        self.assertEqual(r.status_code, 404)
        self.assertIn("no-store", str(r.headers))

    def test_traversal_is_rejected(self):
        (self.shots.parent / "secret.jpg").write_bytes(b"\xff\xd8\xff\xe0x")
        for url in (
            "/static/screenshots/../secret.jpg",
            "/static/screenshots/%2e%2e%2fsecret.jpg",
            "/static/screenshots/..%5Csecret.jpg",
        ):
            r = self.call(url, 1600)
            self.assertEqual(r.status_code, 404, url)

    def test_external_url_is_404(self):
        r = self.call("https://i.ytimg.com/vi/x/maxresdefault.jpg", 1600)
        self.assertEqual(r.status_code, 404)

    def test_other_static_paths_are_404(self):
        r = self.call("/static/cover/x.jpg", 1600)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()

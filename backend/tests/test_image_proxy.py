"""封面图代理（/image_proxy）回归测试（2026-10-17 用户反馈「老笔记封面刷不出来」）。

实测根因与覆盖点：
1. 上游 CDN 单张 3~4s，无缓存时列表上百张封面要几分钟 → 磁盘缓存命中必须
   秒回，且不能再打上游；
2. http://i.ytimg.com/... 这类 http 地址在 CDN 侧挂死，需要 https 变体兜底；
3. 旧实现 302/异常一律 500 且不重试 → 现在跟随重定向、网络级失败重试、
   明确 4xx 不重试（换候选），最终 404 + no-store 让前端回退占位图；
4. 失败地址做短时负缓存，避免每次浏览都去上游撞 3~12s。
"""

import asyncio
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app.routers.note as note_router  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=200, content=b"\xff\xd8\xff\xe0jpeg-bytes", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers if headers is not None else {"Content-Type": "image/jpeg"}


def _fake_client_factory(script, calls, kwargs_log):
    """构造一个按 script 顺序吐响应/抛异常的假 AsyncClient。

    script 元素：_FakeResp / Exception。
    """

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            kwargs_log.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            item = script.pop(0) if script else _FakeResp()
            if isinstance(item, Exception):
                raise item
            return item

    return _FakeAsyncClient


class ImageProxyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = pathlib.Path(self._tmp.name)
        self._patches = [
            patch.object(note_router, "_COVER_CACHE_DIR", self.cache_dir),
            patch.object(note_router, "_COVER_FAIL_CACHE", {}),
            patch.object(note_router, "_cover_cache_prune_tick", 0),
        ]
        for p in self._patches:
            p.start()
        self.request = types.SimpleNamespace(headers={})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def call(self, url):
        return asyncio.run(note_router.image_proxy(self.request, url))


class NormalizeUrlTests(unittest.TestCase):
    def test_protocol_relative_gets_https(self):
        self.assertEqual(
            note_router._normalize_image_url("//i.ytimg.com/vi/x/maxresdefault.jpg"),
            "https://i.ytimg.com/vi/x/maxresdefault.jpg",
        )

    def test_absolute_and_whitespace(self):
        self.assertEqual(
            note_router._normalize_image_url("  http://i0.hdslb.com/a.jpg "),
            "http://i0.hdslb.com/a.jpg",
        )
        self.assertEqual(note_router._normalize_image_url(""), "")


class ImageProxyFetchTests(ImageProxyTestBase):
    def test_success_is_cached_and_second_hit_skips_upstream(self):
        calls, kwargs_log, script = [], [], [_FakeResp()]

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ):
            r1 = self.call("http://i0.hdslb.com/bfs/archive/abc.jpg")
            r2 = self.call("http://i0.hdslb.com/bfs/archive/abc.jpg")

        self.assertEqual(getattr(r1, "status_code", 200), 200)
        self.assertIn("max-age=86400", str(r1.headers))
        self.assertEqual(len(calls), 1, "第二次必须命中磁盘缓存，不再打上游")
        self.assertEqual(getattr(r2, "status_code", 200), 200)
        cached = [p for p in self.cache_dir.glob("*.jpg")]
        self.assertEqual(len(cached), 1, "响应应落盘到缓存目录")
        # 跟随重定向：上游 302 也要能取到图
        self.assertTrue(kwargs_log and kwargs_log[0].get("follow_redirects") is True)

    def test_http_ytimg_falls_back_to_https_candidate(self):
        calls, kwargs_log = [], []
        script = [
            ConnectionError("http hangs"),  # 候选 1 第一次
            ConnectionError("http hangs"),  # 候选 1 重试
            _FakeResp(headers={"Content-Type": "image/jpeg"}),  # 候选 2（https）命中
        ]

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ):
            r = self.call("http://i.ytimg.com/vi/peiuBw3LSZQ/maxresdefault.jpg")

        self.assertEqual(getattr(r, "status_code", 200), 200)
        self.assertEqual(
            calls,
            [
                "http://i.ytimg.com/vi/peiuBw3LSZQ/maxresdefault.jpg",  # 首次网络级失败
                "http://i.ytimg.com/vi/peiuBw3LSZQ/maxresdefault.jpg",  # 同候选重试一次
                "https://i.ytimg.com/vi/peiuBw3LSZQ/maxresdefault.jpg",  # https 变体命中
            ],
        )

    def test_404_does_not_retry_and_negative_cache_short_circuits(self):
        calls, kwargs_log = [], []
        # http 与 https 两个候选各一次：明确 4xx 不重试
        script = [_FakeResp(status_code=404), _FakeResp(status_code=404)]

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ):
            r1 = self.call("http://i0.hdslb.com/bfs/archive/missing.jpg")
            calls_after_first = len(calls)
            r2 = self.call("http://i0.hdslb.com/bfs/archive/missing.jpg")

        self.assertEqual(r1.status_code, 404)
        self.assertIn("no-store", str(r1.headers))
        self.assertEqual(calls_after_first, 2, "明确的 404 不该重试（仅按候选各试一次）")
        self.assertEqual(len(calls), 2, "负缓存命中后不再打上游")
        self.assertEqual(r2.status_code, 404)

    def test_network_error_returns_404_after_retries(self):
        calls, kwargs_log = [], []
        script = [TimeoutError("boom")] * 4  # 两个候选 × 各两次

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ):
            r = self.call("http://i0.hdslb.com/bfs/archive/flaky.jpg")

        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(calls), 4)

    def test_invalid_scheme_is_400(self):
        r = self.call("not-a-url")
        self.assertEqual(r.status_code, 400)

    def test_non_image_content_type_is_coerced(self):
        calls, kwargs_log, script = [], [], [_FakeResp(headers={"Content-Type": "text/html; charset=utf-8"})]

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ):
            r = self.call("http://i0.hdslb.com/bfs/archive/odd.jpg")

        self.assertEqual(getattr(r, "status_code", 200), 200)
        self.assertEqual(r.media_type, "image/jpeg")

    def test_cache_write_failure_still_returns_bytes(self):
        calls, kwargs_log, script = [], [], [_FakeResp()]

        with patch.object(
            note_router.httpx, "AsyncClient", _fake_client_factory(script, calls, kwargs_log)
        ), patch.object(note_router, "_store_cached_cover", return_value=None):
            r = self.call("http://i0.hdslb.com/bfs/archive/nowrite.jpg")

        self.assertEqual(getattr(r, "status_code", 200), 200)
        self.assertEqual(r.body, b"\xff\xd8\xff\xe0jpeg-bytes")


if __name__ == "__main__":
    unittest.main()

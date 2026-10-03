"""前端 dist 直服测试：SPA fallback、静态资源、/api 未命中保持 404 JSON。"""

import pathlib
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.frontend_dist import mount_frontend_dist, resolve_dist_dir
from app.utils.response import ResponseWrapper as R


def _make_dist(tmp: str) -> pathlib.Path:
    dist = pathlib.Path(tmp) / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>viewer</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def _build_app(dist: pathlib.Path) -> FastAPI:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/hello")
    async def hello():
        return R.success(data="hi")

    app = FastAPI()
    app.include_router(router, prefix="/api")
    assert mount_frontend_dist(app, dist_dir=dist) is True
    return app


class TestFrontendDist(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dist = _make_dist(self._tmp.name)
        self.client = TestClient(_build_app(self.dist))

    def tearDown(self):
        self._tmp.cleanup()

    def test_root_serves_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("viewer", r.text)

    def test_spa_fallback_serves_index(self):
        r = self.client.get("/settings/model")
        self.assertEqual(r.status_code, 200)
        self.assertIn("viewer", r.text)

    def test_asset_served(self):
        r = self.client.get("/assets/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("console.log", r.text)

    def test_index_never_cached(self):
        # 手机 Viewer 是浏览器直连：index.html 必须 no-store，
        # 否则后端升级后手机还用旧 index 去拉已不存在的旧分包（404 HTML
        # 当 JS 解析 → 白屏/功能停留在旧版）。
        for path in ("/", "/settings/model"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertIn("no-store", r.headers.get("cache-control", ""))

    def test_api_routes_still_win(self):
        r = self.client.get("/api/hello")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], "hi")

    def test_api_unknown_is_json_404(self):
        r = self.client.get("/api/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], 404)

    def test_missing_dist_returns_false(self):
        app = FastAPI()
        self.assertFalse(
            mount_frontend_dist(app, dist_dir=pathlib.Path(self._tmp.name) / "nodist")
        )

    def test_resolve_explicit_missing_returns_none(self):
        self.assertIsNone(
            resolve_dist_dir(explicit=str(pathlib.Path(self._tmp.name) / "nodist"))
        )


if __name__ == "__main__":
    unittest.main()

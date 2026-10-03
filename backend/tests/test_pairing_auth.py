"""配对鉴权中间件集成测试：走真实 FastAPI app + TestClient。

为避免拉起 lifespan（ffmpeg/DB/调度器），用 lifespan=None 重建最小 app：
挂同样的路由前缀与中间件，只保留 /api/sys_check 与一个哑业务接口。
"""

import pathlib
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.middleware.pairing_auth import (
    PairingAuthMiddleware,
    _client_is_loopback,
    _is_public_path,
)
from app.services.pairing_manager import PairingManager
from app.utils.response import ResponseWrapper as R


def _req(client_host, path="/api/tasks/recent"):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": client_host,
    }
    return Request(scope)


def _build_app(manager: PairingManager) -> FastAPI:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/sys_check")
    async def sys_check():
        return R.success()

    @router.get("/tasks/recent")
    async def tasks_recent():
        return R.success(data=[])

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_middleware(PairingAuthMiddleware, manager=manager)
    return app


class TestPairingAuth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = PairingManager(
            filepath=str(pathlib.Path(self._tmp.name) / "pairing.json")
        )
        self.token = self.manager.get_token()
        self.client = TestClient(_build_app(self.manager))

    def tearDown(self):
        self._tmp.cleanup()

    def test_sys_check_public_without_token(self):
        r = self.client.get("/api/sys_check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)

    def test_api_without_token_rejected(self):
        r = self.client.get("/api/tasks/recent")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], 401)

    def test_api_with_wrong_token_rejected(self):
        r = self.client.get("/api/tasks/recent", headers={"X-Pairing-Token": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_api_with_header_token_allowed(self):
        r = self.client.get("/api/tasks/recent", headers={"X-Pairing-Token": self.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)

    def test_api_with_bearer_token_allowed(self):
        r = self.client.get(
            "/api/tasks/recent", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(r.status_code, 200)

    def test_loopback_detection(self):
        self.assertTrue(_client_is_loopback(_req(("127.0.0.1", 12345))))
        self.assertTrue(_client_is_loopback(_req(("::1", 12345))))
        self.assertFalse(_client_is_loopback(_req(("testclient", 50000))))
        self.assertFalse(_client_is_loopback(_req(("192.168.1.10", 50000))))

    def test_public_path_detection(self):
        for p in ("/api/sys_check", "/api/pairing_status", "/api/pairing_verify"):
            self.assertTrue(_is_public_path(p), p)
        self.assertTrue(_is_public_path("/"))
        self.assertTrue(_is_public_path("/index.html"))
        self.assertTrue(_is_public_path("/static/screenshots/a.jpg"))
        self.assertFalse(_is_public_path("/api/sys_health"))
        self.assertFalse(_is_public_path("/api/generate_note"))

    def test_options_passthrough_without_token(self):
        # CORS 预检不带自定义头，必须放行（否则手机浏览器连预检都发不出去）
        r = self.client.options("/api/tasks/recent")
        self.assertNotEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()

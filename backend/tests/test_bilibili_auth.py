"""B 站扫码登录 + Cookie 自动续期：全 mock 网络，不碰真实 passport API。

覆盖：ac_time_value 解析、SESSDATA 不变时保留旧 refresh_token、旧结构兼容、
扫码成功落盘、刷新全流程（correspond → refresh → confirm → 持久化）、
缺 token / 未登录分支、探测节流。
"""
import json
import re
import time
import types

import pytest

from app.services import bilibili_auth as ba
from app.services.cookie_manager import CookieConfigManager, extract_cookie_value


class FakeResponse:
    def __init__(self, payload=None, cookies=None, text="", status_code=200):
        self._payload = payload if payload is not None else {}
        jar = dict(cookies or {})
        self.cookies = types.SimpleNamespace(get_dict=lambda: jar)
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """按预设路由顺序出响应，同时记录全部调用供断言。"""

    def __init__(self, routes):
        self._routes = list(routes)
        self.calls = []

    def _next(self, method, url, kw):
        self.calls.append((method, url, kw))
        if not self._routes:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self._routes.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, kw)

    def post(self, url, **kw):
        return self._next("POST", url, kw)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return CookieConfigManager()


def _seed_cookie(mgr, cookie, refresh_token=None):
    mgr.set_auth(ba.PLATFORM, cookie, refresh_token=refresh_token)


# ---------------------------------------------------------------------------
# cookie_manager 扩展
# ---------------------------------------------------------------------------

def test_extract_cookie_value_basic():
    cookie = "buvid3=abc; SESSDATA=s1; bili_jct=c1; ac_time_value=t1"
    assert extract_cookie_value(cookie, "SESSDATA") == "s1"
    assert extract_cookie_value(cookie, "ac_time_value") == "t1"
    assert extract_cookie_value(cookie, "missing") is None


def test_set_auth_pulls_ac_time_value_from_pasted_cookie(tmp_config):
    tmp_config.set_auth("bilibili", "SESSDATA=abc; bili_jct=def; ac_time_value=xyz")
    auth = tmp_config.get_auth("bilibili")
    assert auth["refresh_token"] == "xyz"
    assert auth["cookie"].startswith("SESSDATA=abc")


def test_set_auth_preserves_token_when_sessdata_unchanged(tmp_config):
    tmp_config.set_auth("bilibili", "SESSDATA=abc; bili_jct=def", refresh_token="rt1")
    # 用户手动重存同样的 SESSDATA（不带 token）→ 旧 refresh_token 仍有效
    tmp_config.set_auth("bilibili", "SESSDATA=abc; bili_jct=def")
    assert tmp_config.get_auth("bilibili")["refresh_token"] == "rt1"
    # 换了账号（SESSDATA 变了）→ 旧 token 必须清掉，否则刷新必然 86095 失配
    tmp_config.set_auth("bilibili", "SESSDATA=other; bili_jct=def2")
    assert tmp_config.get_auth("bilibili")["refresh_token"] is None


def test_get_auth_backward_compat_old_structure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config" / "downloader.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"bilibili": {"cookie": "SESSDATA=old"}}), encoding="utf-8")
    mgr = CookieConfigManager()
    assert mgr.get("bilibili") == "SESSDATA=old"
    auth = mgr.get_auth("bilibili")
    assert auth["cookie"] == "SESSDATA=old"
    assert auth["refresh_token"] is None


def test_set_keeps_token_for_manual_resave(tmp_config):
    tmp_config.set_auth("bilibili", "SESSDATA=abc; bili_jct=def", refresh_token="rt1")
    tmp_config.set("bilibili", "SESSDATA=abc; bili_jct=def")  # 旧签名 set()
    assert tmp_config.get_auth("bilibili")["refresh_token"] == "rt1"


# ---------------------------------------------------------------------------
# bilibili_auth
# ---------------------------------------------------------------------------

def test_build_cookie_string_merges_and_keeps_device_ids():
    old = "buvid3=dev; SESSDATA=old; bili_jct=c1"
    merged = ba.build_cookie_string(old, {"SESSDATA": "new", "bili_jct": "c2"})
    assert "buvid3=dev" in merged
    assert "SESSDATA=new" in merged
    assert "bili_jct=c2" in merged
    assert "SESSDATA=old" not in merged


def test_correspond_path_is_lowercase_hex_256():
    path = ba._correspond_path(1700000000000)
    assert re.fullmatch(r"[0-9a-f]{256}", path)


def test_poll_qr_login_confirmed_persists_full_credentials(tmp_config, monkeypatch):
    fake = FakeSession([
        FakeResponse(
            {"code": 0, "data": {"code": 0, "refresh_token": "rt1",
             "url": "https://passport.biligame.com/x?SESSDATA=se&bili_jct=bj"}},
            cookies={"SESSDATA": "se", "bili_jct": "bj", "DedeUserID": "1", "sid": "s1"},
        ),
        FakeResponse({"code": 0, "data": {"isLogin": True, "uname": "测试用户"}}),
    ])
    monkeypatch.setattr(ba, "_session", lambda: fake)
    result = ba.poll_qr_login("key123")
    assert result["status"] == "confirmed"
    assert result["username"] == "测试用户"
    assert "SESSDATA=se" in result["cookie"]
    auth = tmp_config.get_auth("bilibili")
    assert auth["refresh_token"] == "rt1"
    assert "SESSDATA=se" in auth["cookie"]
    assert auth["refreshed_at"]


def test_ensure_cookie_fresh_noop_and_throttle(tmp_config, monkeypatch):
    _seed_cookie(tmp_config, "SESSDATA=abc; bili_jct=c1", refresh_token="rt1")
    fake = FakeSession([
        FakeResponse({"code": 0, "data": {"refresh": False, "timestamp": 1}}),
    ])
    monkeypatch.setattr(ba, "_session", lambda: fake)
    first = ba.ensure_cookie_fresh()
    assert first == {"ok": True, "refreshed": False}
    # 节流：紧接着的第二次检查不再打 API
    second = ba.ensure_cookie_fresh()
    assert second.get("skipped") == "recently_checked"
    assert len(fake.calls) == 1


def test_ensure_cookie_fresh_full_refresh_flow(tmp_config, monkeypatch):
    _seed_cookie(tmp_config, "SESSDATA=old; bili_jct=c1; buvid3=dev", refresh_token="rt_old")
    fake = FakeSession([
        FakeResponse({"code": 0, "data": {"refresh": True, "timestamp": 123}}),
        FakeResponse(text='<html><div id="1-name">refreshcsrf123</div></html>'),
        FakeResponse({"code": 0, "data": {"refresh_token": "rt_new"}},
                     cookies={"SESSDATA": "new", "bili_jct": "c2"}),
        FakeResponse({"code": 0}),
        FakeResponse({"code": 0, "data": {"isLogin": True, "uname": "新名"}}),
    ])
    monkeypatch.setattr(ba, "_session", lambda: fake)
    result = ba.ensure_cookie_fresh()
    assert result["ok"] is True
    assert result["refreshed"] is True
    assert result["username"] == "新名"
    auth = tmp_config.get_auth("bilibili")
    assert "SESSDATA=new" in auth["cookie"]
    assert "bili_jct=c2" in auth["cookie"]
    assert "buvid3=dev" in auth["cookie"]  # 设备指纹类 cookie 不丢
    assert auth["refresh_token"] == "rt_new"
    assert auth["refreshed_at"]
    # confirm 用的是新 csrf + 旧 refresh_token（B 站协议要求）
    confirm_call = fake.calls[3]
    assert confirm_call[1] == ba.COOKIE_CONFIRM_URL
    assert confirm_call[2]["data"]["refresh_token"] == "rt_old"
    assert confirm_call[2]["data"]["csrf"] == "c2"


def test_ensure_cookie_fresh_missing_token_points_to_qr_login(tmp_config, monkeypatch):
    _seed_cookie(tmp_config, "SESSDATA=abc; bili_jct=c1")  # 裸 SESSDATA，无 token
    fake = FakeSession([
        FakeResponse({"code": 0, "data": {"refresh": True, "timestamp": 1}}),
    ])
    monkeypatch.setattr(ba, "_session", lambda: fake)
    result = ba.ensure_cookie_fresh()
    assert result["ok"] is False
    assert result["action"] == "qr_login"


def test_ensure_cookie_fresh_not_logged_in(tmp_config, monkeypatch):
    _seed_cookie(tmp_config, "SESSDATA=dead; bili_jct=c1", refresh_token="rt1")
    fake = FakeSession([
        FakeResponse({"code": -101, "message": "账号未登录"}),
    ])
    monkeypatch.setattr(ba, "_session", lambda: fake)
    result = ba.ensure_cookie_fresh()
    assert result["ok"] is False
    assert result["action"] == "qr_login"


def test_ensure_cookie_fresh_no_cookie_at_all(tmp_config):
    result = ba.ensure_cookie_fresh()
    assert result["ok"] is False
    assert result["action"] == "qr_login"

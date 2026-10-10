"""回环地址永不走代理回归测试（2026-10-11）。

根因实锤：后端进程被带 HTTP_PROXY 的终端拉起后继承环境变量 →
ProxyConfigManager.get_proxy_url() 的环境回退返回代理 → LLM 客户端对
http://127.0.0.1:8787/v1（本地 muse 网关）也注入代理 → httpx 经
BeiBeiCore(7892) 访问本机网关被代理层回 502 空响应（curl 走同一代理却
正常），23:49 起全部总结失败、网关日志里没有这些请求。

修复：build_openai_client 对回环 base_url（127.0.0.1 / localhost / ::1）
跳过一切代理注入，恒直连。
"""

import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.utils.openai_client import _effective_proxy_url, _is_loopback_base_url  # noqa: E402

PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


class TestLoopbackDetect(unittest.TestCase):
    def test_loopback_variants(self):
        for u in (
            "http://127.0.0.1:8787/v1",
            "http://localhost:8483/api",
            "http://[::1]:8787/v1",
            "http://127.0.0.1",
        ):
            self.assertTrue(_is_loopback_base_url(u), u)

    def test_remote_or_malformed_not_loopback(self):
        for u in (
            "https://api.groq.com/openai/v1",
            "https://opencode.ai/zen/v1",
            "http://192.168.1.5:8787/v1",
            "",
            None,
        ):
            self.assertFalse(_is_loopback_base_url(u), u)


class TestEffectiveProxyUrl(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in PROXY_ENV_KEYS}
        for k in PROXY_ENV_KEYS:
            os.environ[k] = "http://127.0.0.1:7892"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_proxy_ignored_for_loopback(self):
        """即使环境里 HTTP_PROXY 齐全，回环地址也必须解析为 None（直连）。"""
        self.assertIsNone(_effective_proxy_url("http://127.0.0.1:8787/v1"))
        self.assertIsNone(_effective_proxy_url("http://localhost:8787/v1"))

    def test_env_proxy_still_used_for_remote(self):
        """远端地址仍按原语义吃环境变量回退（docker/CLI 场景不回归）。"""
        with patch("app.utils.openai_client.ProxyConfigManager") as mgr:
            mgr.return_value.get_proxy_url.return_value = "http://127.0.0.1:7892"
            self.assertEqual(
                _effective_proxy_url("https://api.groq.com/openai/v1"),
                "http://127.0.0.1:7892",
            )

    def test_loopback_short_circuits_before_manager(self):
        """回环短路发生在读配置/环境之前——ProxyConfigManager 不应被调用。"""
        with patch("app.utils.openai_client.ProxyConfigManager") as mgr:
            self.assertIsNone(_effective_proxy_url("http://127.0.0.1:8787/v1"))
            mgr.assert_not_called()


if __name__ == "__main__":
    unittest.main()

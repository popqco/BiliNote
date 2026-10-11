"""供应商「自定义请求头」链路 + B 站直连 Session 单测（2026-10-11）。

背景：
- OpenCode Go 网关要求每会话带稳定 x-opencode-session 头，缺失报 400
  MissingSessionID；BiliNote 供应商链路此前没有自定义头通道。
- B 站 API 一律直连（trust_env=False），本地代理进程死了不再拖垮自动化。
"""
import unittest
from unittest.mock import patch

from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider, parse_extra_headers
from app.utils.bilibili_http import bilibili_session
from app.utils import openai_client as oai


class TestParseExtraHeaders(unittest.TestCase):
    def test_valid_json_object(self):
        self.assertEqual(
            parse_extra_headers('{"x-opencode-session": "abc-123"}'),
            {"x-opencode-session": "abc-123"},
        )

    def test_empty_and_whitespace(self):
        self.assertIsNone(parse_extra_headers(None))
        self.assertIsNone(parse_extra_headers(""))
        self.assertIsNone(parse_extra_headers("   "))

    def test_invalid_json_ignored_not_raised(self):
        self.assertIsNone(parse_extra_headers("{not json"))
        self.assertIsNone(parse_extra_headers('{"a": 1,}'))

    def test_non_object_ignored(self):
        self.assertIsNone(parse_extra_headers('["a", "b"]'))
        self.assertIsNone(parse_extra_headers('"str"'))
        self.assertIsNone(parse_extra_headers("123"))

    def test_values_coerced_and_empty_keys_dropped(self):
        self.assertEqual(
            parse_extra_headers('{"k": 123, " ": "x", "k2": true}'),
            {"k": "123", "k2": "True"},
        )
        self.assertIsNone(parse_extra_headers('{" ": "only-empty-key"}'))


class TestBuildOpenAIClientHeaders(unittest.TestCase):
    def test_default_headers_passed_to_openai(self):
        with patch.object(oai, "OpenAI") as fake_openai, \
                patch.object(oai, "ProxyConfigManager") as mgr, \
                patch.object(oai, "proxy_reachable", return_value=True):
            mgr.return_value.get_proxy_url.return_value = None
            oai.build_openai_client(
                "sk-test", "https://api.example.com/v1",
                default_headers={"x-opencode-session": "abc"},
            )
            kwargs = fake_openai.call_args.kwargs
            self.assertEqual(kwargs.get("default_headers"), {"x-opencode-session": "abc"})

    def test_no_headers_no_key(self):
        with patch.object(oai, "OpenAI") as fake_openai, \
                patch.object(oai, "ProxyConfigManager") as mgr, \
                patch.object(oai, "proxy_reachable", return_value=True):
            mgr.return_value.get_proxy_url.return_value = None
            oai.build_openai_client("sk-test", "https://api.example.com/v1")
            self.assertNotIn("default_headers", fake_openai.call_args.kwargs)

    def test_provider_parses_headers_into_client(self):
        with patch.object(oai, "OpenAI") as fake_openai, \
                patch.object(oai, "ProxyConfigManager") as mgr, \
                patch.object(oai, "proxy_reachable", return_value=True):
            mgr.return_value.get_proxy_url.return_value = None
            OpenAICompatibleProvider(
                "sk-test", "https://api.example.com/v1",
                extra_headers='{"x-opencode-session": "sess-1"}',
            )
            self.assertEqual(
                fake_openai.call_args.kwargs.get("default_headers"),
                {"x-opencode-session": "sess-1"},
            )

    def test_provider_bad_headers_falls_back_to_no_headers(self):
        with patch.object(oai, "OpenAI") as fake_openai, \
                patch.object(oai, "ProxyConfigManager") as mgr, \
                patch.object(oai, "proxy_reachable", return_value=True):
            mgr.return_value.get_proxy_url.return_value = None
            OpenAICompatibleProvider(
                "sk-test", "https://api.example.com/v1", extra_headers="not-json",
            )
            self.assertNotIn("default_headers", fake_openai.call_args.kwargs)


class TestBilibiliSession(unittest.TestCase):
    def test_direct_connection_enforced(self):
        s = bilibili_session()
        self.assertFalse(s.trust_env)
        self.assertIn("User-Agent", s.headers)
        self.assertIn("Mozilla", s.headers["User-Agent"])

    def test_watchlater_module_uses_helper(self):
        """watchlater 不再 import requests 裸调（代理免疫的静态保证）。"""
        import app.services.watchlater as wl

        self.assertFalse(hasattr(wl, "requests"))
        self.assertTrue(hasattr(wl, "bilibili_session"))


if __name__ == "__main__":
    unittest.main()

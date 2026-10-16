"""MCP server 回环直连回归测试（2026-10-16）。

实拍：Clash 等系统代理开启（Windows 注册表 ProxyEnable=1）时，httpx 的
trust_env 会读系统代理且不尊重 ProxyOverride 例外列表，把
http://127.0.0.1:8483 的请求塞给代理层（7892）拿回 502 空响应——MCP
list_notes 全挂在 502。修复：BILINOTE_BASE_URL 为回环地址时
trust_env=False（关掉 env+注册表两路代理），远端地址语义不变。
"""

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    import mcp_server  # noqa: E402
    _MCP_IMPORT_ERROR = None
except Exception as e:  # mcp 包缺失的环境直接跳过
    mcp_server = None
    _MCP_IMPORT_ERROR = e


@unittest.skipIf(mcp_server is None, f"mcp_server 不可导入: {_MCP_IMPORT_ERROR}")
class TestMcpLoopbackDirect(unittest.TestCase):
    def test_loopback_detector(self):
        for u in (
            "http://127.0.0.1:8483",
            "http://localhost:8483",
            "http://[::1]:8483",
        ):
            self.assertTrue(mcp_server._is_loopback_url(u), u)
        for u in (
            "http://100.67.206.88:8483",
            "https://example.com",
            "",
            "not a url",
        ):
            self.assertFalse(mcp_server._is_loopback_url(u), u)

    def test_client_bypasses_proxy_for_loopback(self):
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "no_proxy", "NO_PROXY")}
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:7892"
            os.environ["http_proxy"] = "http://127.0.0.1:7892"
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            c = mcp_server._client()
            self.assertFalse(c._trust_env)  # 代理 env/注册表都不读
            self.assertEqual(c._mounts, {})  # 没有任何代理挂载
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_client_keeps_trust_env_for_remote(self):
        # 远端地址语义不变（仍走 env 代理配置）
        old_base = mcp_server.BASE_URL
        old_no_proxy = os.environ.pop("no_proxy", None)
        try:
            mcp_server.BASE_URL = "http://100.67.206.88:8483"
            os.environ.pop("NO_PROXY", None)
            c = mcp_server._client()
            self.assertTrue(c._trust_env)
        finally:
            mcp_server.BASE_URL = old_base
            if old_no_proxy is not None:
                os.environ["no_proxy"] = old_no_proxy


if __name__ == "__main__":
    unittest.main()

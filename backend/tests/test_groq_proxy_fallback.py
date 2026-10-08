"""Groq 转写「直连 403 → 系统代理重试」降级路径测试。

背景（2026-10-08 实锤）：某中转站对国内直连 IP 一律 403，同 key 走系统
代理即 200；应用客户端默认 trust_env=False 绕系统代理，转写被误伤。
groq.py 现在遇 PermissionDeniedError 会用 use_system_proxy=True 重建
客户端重试一次。本测试用桩客户端验证两条分支。

约定遵循 tests/ 现有风格：unittest + importlib 按文件加载被测模块。
"""

import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_groq_module():
    """加载 transcriber/groq.py，重依赖全部用桩。返回 (模块, 记账字典)。"""
    calls = {"builds": [], "creates": []}

    import logging

    # 包骨架
    for name in ("app", "app.decorators", "app.models", "app.services",
                 "app.transcriber", "app.utils"):
        pkg = types.ModuleType(name)
        pkg.__path__ = []
        sys.modules[name] = pkg

    logger_mod = types.ModuleType("app.utils.logger")
    logger_mod.get_logger = logging.getLogger
    sys.modules["app.utils.logger"] = logger_mod

    timeit_mod = types.ModuleType("app.decorators.timeit")
    timeit_mod.timeit = lambda fn: fn
    sys.modules["app.decorators.timeit"] = timeit_mod

    tm = types.ModuleType("app.models.transcriber_model")

    class TranscriptResult:
        def __init__(self, language, full_text, segments, raw=None):
            self.language = language
            self.full_text = full_text
            self.segments = segments
            self.raw = raw

    class TranscriptSegment:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    tm.TranscriptResult = TranscriptResult
    tm.TranscriptSegment = TranscriptSegment
    sys.modules["app.models.transcriber_model"] = tm

    pv = types.ModuleType("app.services.provider")

    class ProviderService:
        @staticmethod
        def get_provider_by_id(pid):
            return {"id": "groq", "api_key": "gsk_test", "base_url": "https://relay.test/v1"}

    pv.ProviderService = ProviderService
    sys.modules["app.services.provider"] = pv

    base_mod = types.ModuleType("app.transcriber.base")

    class Transcriber:
        pass

    base_mod.Transcriber = Transcriber
    sys.modules["app.transcriber.base"] = base_mod

    # build_openai_client 桩：记账参数 + 返回预置客户端
    oai = types.ModuleType("app.utils.openai_client")
    state = {"clients": []}

    def build_openai_client(api_key, base_url, *, key_label="", timeout=None,
                            use_system_proxy=False):
        calls["builds"].append({"use_system_proxy": use_system_proxy})
        idx = len(calls["builds"]) - 1
        return state["clients"][idx]

    oai.build_openai_client = build_openai_client
    sys.modules["app.utils.openai_client"] = oai

    spec = importlib.util.spec_from_file_location(
        "groq_transcriber_under_test", ROOT / "app" / "transcriber" / "groq.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["groq_transcriber_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod, calls, state


def _make_client(behavior, calls):
    """构造假 OpenAI 客户端：behavior='ok' 或 'deny'。"""

    class _FakeTranscriptions:
        def create(self, file=None, model=None, response_format=None):
            calls["creates"].append({"model": model})
            if behavior == "deny":
                import httpx
                from openai import PermissionDeniedError

                resp = httpx.Response(
                    403, request=httpx.Request("POST", "https://relay.test/v1/audio/transcriptions")
                )
                raise PermissionDeniedError(
                    "Error code: 403 - {'error': {'message': 'Forbidden'}}",
                    response=resp,
                    body=None,
                )

            class _Seg:
                start, end, text = 0.0, 1.0, "你好"

            class _T:
                text = "你好 世界"
                language = "zh"
                segments = [_Seg()]

                def to_dict(self):
                    return {"text": self.text}

            return _T()

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        audio = _FakeAudio()

    return _FakeClient()


class TestGroqProxyFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.calls, cls.state = _load_groq_module()
        # 造一个 1KB 的假音频文件，避免真实下载/压缩
        import tempfile, os
        fd, cls.audio_path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\0" * 1024)

    def setUp(self):
        self.calls["builds"].clear()
        self.calls["creates"].clear()
        self.state["clients"].clear()

    def test_direct_403_falls_back_to_system_proxy(self):
        self.state["clients"].extend([
            _make_client("deny", self.calls),
            _make_client("ok", self.calls),
        ])
        result = self.mod.GroqTranscriber().transcript(self.audio_path)
        self.assertEqual(result.language, "zh")
        self.assertEqual(len(self.calls["builds"]), 2)
        self.assertFalse(self.calls["builds"][0]["use_system_proxy"])
        self.assertTrue(self.calls["builds"][1]["use_system_proxy"])
        self.assertEqual(len(self.calls["creates"]), 2)

    def test_direct_success_no_fallback(self):
        self.state["clients"].append(_make_client("ok", self.calls))
        result = self.mod.GroqTranscriber().transcript(self.audio_path)
        self.assertEqual(result.full_text, "你好")
        self.assertEqual(len(self.calls["builds"]), 1)
        self.assertFalse(self.calls["builds"][0]["use_system_proxy"])

    def test_proxy_retry_also_403_propagates(self):
        from openai import PermissionDeniedError

        self.state["clients"].extend([
            _make_client("deny", self.calls),
            _make_client("deny", self.calls),
        ])
        with self.assertRaises(PermissionDeniedError):
            self.mod.GroqTranscriber().transcript(self.audio_path)
        self.assertEqual(len(self.calls["builds"]), 2)


if __name__ == "__main__":
    unittest.main()

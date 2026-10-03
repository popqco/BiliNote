"""Responses API 适配层单元测试。

不依赖真实网络：用假 inner client 记录 create() 收到的参数、返回
预制的 Responses 形态对象/事件，验证 chat → responses 的翻译与回包
包装（system→instructions、tools 平铺、tool_calls 往返、流式增量、
多模态图片）。
"""

import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_compat():
    # 轻量 logger stub，避免 import app.* 重型依赖链（同 test_cross_note_qa 风格）
    import logging

    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    utils_pkg = types.ModuleType("app.utils")
    utils_pkg.__path__ = []
    logger_mod = types.ModuleType("app.utils.logger")
    logger_mod.get_logger = lambda name: logging.getLogger(name)
    gpt_pkg = types.ModuleType("app.gpt")
    gpt_pkg.__path__ = []
    provider_pkg = types.ModuleType("app.gpt.provider")
    provider_pkg.__path__ = []
    for name, mod in {
        "app": app_pkg,
        "app.utils": utils_pkg,
        "app.utils.logger": logger_mod,
        "app.gpt": gpt_pkg,
        "app.gpt.provider": provider_pkg,
    }.items():
        sys.modules.setdefault(name, mod)

    module_name = "responses_compat"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "app" / "gpt" / "provider" / "responses_compat.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


compat = _load_compat()


class FakeResponses:
    """记录 create 参数并可注入响应/事件序列的假 responses 资源。"""

    def __init__(self, result=None, events=None):
        self.result = result
        self.events = events or []
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.events:
            return iter(self.events)
        return self.result


class FakeClient(SimpleNamespace):
    pass


def _resp_message(text="答案内容"):
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ]
    )


class TestResponsesCompat(unittest.TestCase):
    def test_system_becomes_instructions(self):
        fake = FakeResponses(result=_resp_message())
        client = compat.ResponsesCompatClient(
            FakeClient(responses=fake, models=object())
        )
        client.chat.completions.create(
            model="m",
            messages=[
                {"role": "system", "content": "你是问答助手"},
                {"role": "user", "content": "问题"},
            ],
            temperature=0.7,
        )
        body = fake.calls[0]
        self.assertEqual(body["instructions"], "你是问答助手")
        self.assertEqual(
            body["input"],
            [{"type": "message", "role": "user", "content": "问题"}],
        )
        # temperature 非 None 要透传（Responses 接受该参数）
        self.assertEqual(body.get("temperature"), 0.7)

    def test_wrap_response_text(self):
        fake = FakeResponses(result=_resp_message("1+1等于2。"))
        client = compat.ResponsesCompatClient(FakeClient(responses=fake))
        out = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "1+1=?"}]
        )
        self.assertEqual(out.choices[0].message.content, "1+1等于2。")
        self.assertIsNone(out.choices[0].message.tool_calls)

    def test_tools_flattened_and_roundtrip(self):
        fn_call_item = SimpleNamespace(
            type="function_call",
            call_id="call_1",
            name="get_video_info",
            arguments='{"x": 1}',
        )
        fake = FakeResponses(
            result=SimpleNamespace(output=[fn_call_item])
        )
        client = compat.ResponsesCompatClient(FakeClient(responses=fake))
        chat_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_video_info",
                    "description": "获取视频元信息",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        out = client.chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "视频作者是谁"}],
            tools=chat_tools,
        )
        # 请求侧：chat 嵌套格式 → responses 平铺
        self.assertEqual(
            fake.calls[0]["tools"],
            [
                {
                    "type": "function",
                    "name": "get_video_info",
                    "description": "获取视频元信息",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        )
        # 响应侧：function_call 项 → chat tool_calls 形态
        tc = out.choices[0].message.tool_calls[0]
        self.assertEqual(tc.id, "call_1")
        self.assertEqual(tc.function.name, "get_video_info")
        self.assertEqual(tc.function.arguments, '{"x": 1}')

        # 下一轮：assistant(tool_calls) + role=tool 回包 → function_call(_output)
        assistant_msg = out.choices[0].message
        client.chat.completions.create(
            model="m",
            messages=[
                {"role": "user", "content": "q"},
                assistant_msg,
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"uploader": "tester"}',
                },
            ],
            tools=chat_tools,
        )
        items = fake.calls[1]["input"]
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[1]["call_id"], "call_1")
        self.assertEqual(items[2]["type"], "function_call_output")
        self.assertEqual(items[2]["call_id"], "call_1")

    def test_image_url_translated(self):
        fake = FakeResponses(result=_resp_message())
        client = compat.ResponsesCompatClient(FakeClient(responses=fake))
        client.chat.completions.create(
            model="m",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://x/img.jpg", "detail": "auto"},
                        },
                    ],
                }
            ],
        )
        parts = fake.calls[0]["input"][0]["content"]
        self.assertEqual(parts[0], {"type": "input_text", "text": "看图"})
        self.assertEqual(
            parts[1],
            {"type": "input_image", "image_url": "https://x/img.jpg", "detail": "auto"},
        )

    def test_stream_text_and_reasoning(self):
        events = [
            SimpleNamespace(type="response.reasoning_text.delta", delta="思考中"),
            SimpleNamespace(type="response.output_text.delta", delta="你好"),
            SimpleNamespace(type="response.output_text.delta", delta="呀"),
            SimpleNamespace(type="response.completed", response=None),
        ]
        fake = FakeResponses(events=events)
        client = compat.ResponsesCompatClient(FakeClient(responses=fake))
        chunks = list(
            client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        )
        texts = [c.choices[0].delta.content for c in chunks]
        self.assertEqual(texts, [None, "你好", "呀", None])
        self.assertEqual(chunks[0].choices[0].delta.reasoning_content, "思考中")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_models_passthrough(self):
        marker = object()
        client = compat.ResponsesCompatClient(FakeClient(models=marker, responses=None))
        self.assertIs(client.models, marker)


if __name__ == "__main__":
    unittest.main()

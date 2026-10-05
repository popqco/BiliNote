"""上游抖动两连击的回归测试（2026-10-05 两条笔记连败实录）：

1. 免费网关间歇性空回：流式迭代在 openai._streaming.json 里抛
   JSONDecodeError（"Expecting value: line 1 column 1 (char 0)"），原先既不是
   EmptyCompletionError 也没进 _is_retryable_error，三档降级策略全一次判死、
   零重试——而同窗口其他任务稍后成功，证明一次重试本就能救回来。
2. Responses 流里的异常事件（response.failed / response.incomplete /
   response.incomplete 等）原先被静默吞掉，_stream_create 只看到"流结束
   + 无正文"，报 EmptyCompletionError，真实原因（上游限流/截断）丢了。
"""
import sys
import types
from unittest.mock import patch

import pytest

from app.gpt import universal_gpt as ug
from app.gpt.provider import responses_compat as rc


def _exc_chat_client(exc):
    """chat.completions.create 一调用就抛 exc 的伪 client。"""

    class C:
        def __init__(self):
            self.calls = 0
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self.create)
            )

        def create(self, **kwargs):
            self.calls += 1
            raise exc

    return C()


def _gpt_with_client(client):
    with patch.object(ug.UniversalGPT, "__init__", lambda self, *a, **k: None):
        g = ug.UniversalGPT.__new__(ug.UniversalGPT)
    g.model = "m"
    g.temperature = 0.7
    g.client = client
    g._max_retry_attempts = 3
    g._retry_base_backoff = 0
    return g


def _json_decode_error():
    import json

    try:
        json.loads("")
    except json.JSONDecodeError as e:
        return e
    raise AssertionError("unreachable")


def test_json_decode_error_is_retryable_and_recovers():
    """空响应体 JSONDecodeError：第一次抖、第二次好 → 总调用成功。"""
    good = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")]
    )
    g = _gpt_with_client(_exc_chat_client(_json_decode_error()))
    attempts = {"n": 0}

    def do_create(messages):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _json_decode_error()
        return good

    g._do_create = do_create
    out = g._chat_completion_create([{"role": "user", "content": "hi"}])
    assert out.choices[0].message.content == "ok"
    assert attempts["n"] == 2


def test_json_decode_error_exhausts_and_raises():
    """一直空回：重试打满后异常向上传（交给降级阶梯换更小的请求）。"""
    g = _gpt_with_client(_exc_chat_client(_json_decode_error()))
    g._do_create = lambda messages: (_ for _ in ()).throw(_json_decode_error())
    with pytest.raises(Exception):
        g._chat_completion_create([{"role": "user", "content": "hi"}])


def test_stream_failure_event_raises_with_reason():
    """response.failed 事件不再被吞：带出上游错误原文。"""
    err = types.SimpleNamespace(message="rate_limited_by_upstream")
    resp = types.SimpleNamespace(error=err)
    events = [types.SimpleNamespace(type="response.failed", response=resp)]

    class Inner:
        class responses:
            @staticmethod
            def create(**kwargs):
                return iter(events)

    comp = rc._Completions(Inner())
    with pytest.raises(RuntimeError, match="rate_limited_by_upstream"):
        list(comp._stream({"model": "m", "input": []}))


def test_stream_incomplete_event_raises():
    """response.incomplete（截断/限流掐断）同样显式报错，不再伪装成空流。"""
    events = [types.SimpleNamespace(type="response.incomplete", response=None)]
    comp = rc._Completions(types.SimpleNamespace(
        responses=types.SimpleNamespace(create=lambda **k: iter(events))
    ))
    with pytest.raises(RuntimeError, match="incomplete"):
        list(comp._stream({"model": "m", "input": []}))

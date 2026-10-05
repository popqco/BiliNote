"""OpenAI Responses API → Chat Completions 形态的客户端适配层。

为什么需要：部分供应商 / 本地代理（OpenCode 系网关、Codex 生态的
cc-switch 等）只提供 OpenAI **Responses API**（``POST /v1/responses``），
不实现 ``/v1/chat/completions``——直接调后者上游会回
``HTTP 500 → 代理 502 proxy_error``（2026-10-03 实测 muse-spark-1.3-
contributor-free 经 OpenCode Free 本地代理 127.0.0.1:8787 即如此；
同代理走 /responses 一切正常）。ZCode / cc-switch 对这类供应商都是把
wire api 切到 responses 解决的。

这里按供应商级 ``api_format='responses'``（providers 表新列，设置页可选）
把整个调用面翻译过去，UniversalGPT / chat_service 的调用代码零改动：

- messages：system/developer → 顶层 ``instructions``（很多 Responses 模型
  **不支持对话中的 system 消息**，ZCode 模型配置里 muse-spark 就没勾
  “对话中系统消息”，这也是 chat/completions 链路 500 的成因之一）；
  普通消息 → ``input`` 项；assistant 携带 tool_calls → ``function_call``
  项；role=tool → ``function_call_output`` 项。
- 多模态：``image_url`` → ``input_image``（截图总结走 UniversalGPT 流式
  也被覆盖）。
- tools：chat 嵌套格式 ``{type:function, function:{...}}`` → Responses
  平铺格式 ``{type:function, name, description, parameters}``。
- 响应：``output`` 里的 message/function_call 项包装回
  ``choices[0].message.{content, tool_calls}`` 形态。
- 流式：``response.output_text.delta`` → ``choices[0].delta.content``；
  reasoning 增量 → ``delta.reasoning_content``（UniversalGPT 的假死判定
  把它算作“上游还活着”）。

注意该适配器只是**协议翻译**，不做工具执行；function calling 循环
（chat_service）与非流式/流式补全（UniversalGPT）照旧工作。
"""

from types import SimpleNamespace

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _msg_field(msg, name, default=None):
    """消息既可能是 dict（常规路径）也可能是上一轮包装出的
    SimpleNamespace（chat_service 把 response.choices[0].message 原样
    append 回 messages），取字段时两种都兼容。"""
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def _translate_content(content):
    """chat content（str 或多模态数组）→ Responses 输入 content。

    纯字符串原样传（Responses 接受 string content）；数组里 text →
    input_text、image_url → input_image（含 detail 透传），其余类型
    （已是 input_* / output_text 形态的）原样保留。
    """
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, SimpleNamespace):
        content = vars(content)
    if not isinstance(content, list):
        return str(content)
    parts = []
    for p in content:
        if isinstance(p, SimpleNamespace):
            p = vars(p)
        if not isinstance(p, dict):
            parts.append({"type": "input_text", "text": str(p)})
            continue
        ptype = p.get("type")
        if ptype == "text":
            parts.append({"type": "input_text", "text": p.get("text", "")})
        elif ptype == "image_url":
            img = p.get("image_url") or {}
            url = img.get("url", "") if isinstance(img, dict) else str(img)
            part = {"type": "input_image", "image_url": url}
            detail = img.get("detail") if isinstance(img, dict) else None
            if detail:
                part["detail"] = detail
            parts.append(part)
        elif ptype in ("input_text", "input_image", "output_text"):
            parts.append(p)
        # 其余类型（input_audio 等）本项目用不到，直接丢弃
    return parts or None


def _translate_messages(messages):
    """chat messages → (instructions, input_items)。

    system/developer 消息合并进顶层 instructions（保序、按出现顺序拼接）；
    其余按顺序转成 input 项。工具往返（assistant.tool_calls / role=tool）
    翻译成 function_call / function_call_output。
    """
    instructions_parts: list[str] = []
    input_items: list[dict] = []

    for msg in messages or []:
        role = _msg_field(msg, "role", "")
        content = _msg_field(msg, "content")

        if role in ("system", "developer"):
            if isinstance(content, str):
                instructions_parts.append(content)
            else:
                flat = _translate_content(content)
                if isinstance(flat, str):
                    instructions_parts.append(flat)
                elif flat:
                    instructions_parts.append(
                        "\n".join(
                            p.get("text", "") for p in flat if p.get("type") == "input_text"
                        )
                    )
            continue

        if role == "tool":
            call_id = _msg_field(msg, "tool_call_id", "")
            output = content if isinstance(content, str) else str(content)
            input_items.append(
                {"type": "function_call_output", "call_id": call_id, "output": output}
            )
            continue

        if role == "assistant" or _msg_field(msg, "tool_calls"):
            tool_calls = _msg_field(msg, "tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, SimpleNamespace):
                        tc = vars(tc)
                    fn = tc.get("function") or {}
                    if isinstance(fn, SimpleNamespace):
                        fn = vars(fn)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id") or "",
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "") or "{}",
                        }
                    )
                # 带工具调用的 assistant 消息通常没有正文；有也照发
                if content:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": _translate_content(content),
                        }
                    )
                continue

        input_items.append(
            {
                "type": "message",
                "role": role or "user",
                "content": _translate_content(content),
            }
        )

    instructions = "\n\n".join(p for p in instructions_parts if p and p.strip())
    return (instructions or None), input_items


def _translate_tool(tool):
    """chat 嵌套 function 定义 → Responses 平铺 function 定义。"""
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return tool
    fn = tool.get("function") or {}
    return {
        "type": "function",
        "name": fn.get("name"),
        "description": fn.get("description", ""),
        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _item_text(item) -> str:
    parts = []
    for c in getattr(item, "content", None) or []:
        ctype = getattr(c, "type", None)
        if ctype in ("output_text", "text"):
            parts.append(getattr(c, "text", "") or "")
    return "".join(parts)


def _wrap_response(resp) -> SimpleNamespace:
    """Responses 非流式响应 → chat.completions 同形对象。

    只取调用方真正读取的字段（choices[0].message.content / tool_calls）；
    tool_call 的 id 用 call_id（下一轮 function_call_output 要回传同一个）。
    """
    text_parts: list[str] = []
    tool_calls: list[SimpleNamespace] = []
    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", None)
        if itype == "message":
            t = _item_text(item)
            if t:
                text_parts.append(t)
        elif itype == "function_call":
            tool_calls.append(
                SimpleNamespace(
                    id=getattr(item, "call_id", None) or getattr(item, "id", ""),
                    type="function",
                    function=SimpleNamespace(
                        name=getattr(item, "name", "") or "",
                        arguments=getattr(item, "arguments", "") or "{}",
                    ),
                )
            )
    content = "".join(text_parts)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    # role 必须带：chat_service 会把本对象原样 append 回
                    # messages 进入下一轮翻译，缺 role 会被当成 user。
                    role="assistant",
                    content=content or None,
                    tool_calls=tool_calls or None,
                ),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
    )


def _chunk(delta_content=None, reasoning=None, finish_reason=None) -> SimpleNamespace:
    delta = SimpleNamespace(content=delta_content, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
    )


class _Completions:
    """伪 ``client.chat.completions``：create() 翻译到 inner.responses.create()。"""

    def __init__(self, inner):
        self._inner = inner

    def create(self, model, messages=None, tools=None, temperature=None,
               stream=False, **kwargs):
        instructions, input_items = _translate_messages(messages or [])
        body: dict = {"model": model, "input": input_items}
        if instructions:
            body["instructions"] = instructions
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [_translate_tool(t) for t in tools]
        if kwargs.get("max_tokens") is not None:
            # Responses 用 max_output_tokens，且预算含推理 token；
            # 取调用方原值即可，太小的兜底交给上游报错。
            body["max_output_tokens"] = kwargs["max_tokens"]

        if stream:
            return self._stream(body)
        resp = self._inner.responses.create(**body)
        return _wrap_response(resp)

    def _stream(self, body: dict):
        stream = self._inner.responses.create(stream=True, **body)
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                if delta:
                    yield _chunk(delta_content=delta)
            elif etype in (
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            ):
                # 推理增量也算活跃：流式假死闸（UniversalGPT）认
                # reasoning_content，只认 content 会把“正在思考”误判假死。
                delta = getattr(event, "delta", "") or ""
                if delta:
                    yield _chunk(reasoning=delta)
            elif etype == "response.completed":
                yield _chunk(finish_reason="stop")
            elif etype == "response.failed":
                resp = getattr(event, "response", None)
                err = getattr(resp, "error", None)
                raise RuntimeError(
                    f"Responses 上游失败: {getattr(err, 'message', None) or err}"
                )
            elif etype in ("response.incomplete", "response.incompleted"):
                # 截断/限流掐断：原来直接被吞，调用方只看到"流结束+无正文"的
                # EmptyCompletionError，真实原因丢了（2026-10-05 连败复盘发现）。
                resp = getattr(event, "response", None)
                reason = (
                    getattr(getattr(resp, "incomplete_details", None), "reason", None)
                    or getattr(resp, "status", None)
                    or etype
                )
                raise RuntimeError(f"Responses 上游未完成: {reason}")


class ResponsesCompatClient:
    """包一层 OpenAI SDK client，把 .chat.completions 指到 Responses API。

    其余属性（.models.list 等）原样透传，模型列表拉取不受影响。
    """

    def __init__(self, inner):
        # 只用 object.__setattr__，避免 __getattr__ 递归
        object.__setattr__(self, "_inner", inner)

    @property
    def chat(self):
        return SimpleNamespace(completions=_Completions(self._inner))

    def __getattr__(self, name):
        return getattr(self._inner, name)

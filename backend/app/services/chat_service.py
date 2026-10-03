import json
from typing import Optional

from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.services.provider import ProviderService
from app.services.vector_store import VectorStoreManager
from app.services.chat_tools import TOOLS, execute_tool
from app.utils.logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """你是一个视频笔记问答助手。你拥有以下能力：

1. 系统已自动检索了一些相关内容作为初始参考（见下方）
2. 你可以调用工具主动查询更多信息：
   - lookup_transcript: 查询视频原始转录文本（支持按时间、关键词、位置筛选）
   - get_video_info: 获取视频元信息（标题、作者、简介、标签等）
   - get_note_content: 获取完整笔记内容

--- 初始检索内容 ---
{context}
---

回答要求：
- 如果初始检索内容不足以回答问题，请主动调用工具获取更多信息
- 回答关于视频具体原话、细节时，用 lookup_transcript 查询原文
- 回答关于作者、标题等基本信息时，用 get_video_info 查询
- 初始检索可能来自多篇笔记（片段标签带《标题》前缀）：引用跨笔记内容时
  请注明出自哪篇笔记（用《标题》），不要把不同笔记的内容混为一篇
- 请用中文回答，保持简洁准确"""


def _build_context(chunks: list[dict]) -> str:
    """将检索到的片段拼接为上下文文本。

    跨笔记片段（metadata 带 note_title/task_id）会在标签前加上
    ``《标题》`` 前缀，方便 LLM 在回答中标出来源；单篇旧索引没有
    这些字段，标签与历史行为一致。
    """
    parts = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source_type = meta.get("source_type", "unknown")
        title_prefix = f"《{meta['note_title']}》" if meta.get("note_title") else ""
        if source_type == "meta":
            label = f"{title_prefix}[视频信息]"
        elif source_type == "markdown":
            label = f"{title_prefix}[笔记 - {meta.get('section_title', '')}]"
        else:
            start = meta.get("start_time", 0)
            end = meta.get("end_time", 0)
            label = f"{title_prefix}[转录 - {start:.0f}s~{end:.0f}s]"
        parts.append(f"{label}\n{chunk['text']}")
    return "\n\n".join(parts)


def _build_sources(chunks: list[dict]) -> list[dict]:
    """从检索片段中提取来源信息（含跨笔记的 task_id / note_title）。"""
    sources = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source = {
            "text": chunk["text"][:200],
            "source_type": meta.get("source_type", "unknown"),
        }
        if meta.get("task_id"):
            source["task_id"] = meta["task_id"]
        if meta.get("note_title"):
            source["note_title"] = meta["note_title"]
        if meta.get("section_title"):
            source["section_title"] = meta["section_title"]
        if meta.get("start_time") is not None:
            source["start_time"] = meta["start_time"]
        if meta.get("end_time") is not None:
            source["end_time"] = meta["end_time"]
        sources.append(source)
    return sources


def _call_llm(gpt, messages: list, use_tools: bool):
    """单轮 LLM 调用，带两层就地降级。

    免费推理模型（muse-spark 等）常见两类不兼容：不接受自定义
    temperature、不支持 function calling——直接把原始报错抛给用户只会
    让人以为“模型坏了”。这里识别这两类报错后去掉对应参数重试一次；
    去掉 tools 后模型不会再回工具调用，本轮直接当最终回答返回。
    """
    kwargs = {"model": gpt.model, "messages": messages}
    if use_tools:
        kwargs["tools"] = TOOLS
    try:
        return gpt.client.chat.completions.create(temperature=0.7, **kwargs)
    except Exception as exc:
        raw = str(exc).lower()
        if "temperature" in raw and (
            "does not support" in raw or "unsupported" in raw or "only the default" in raw
        ):
            logger.warning(f"模型 {gpt.model} 不支持自定义 temperature，去参重试")
            return gpt.client.chat.completions.create(**kwargs)
        if use_tools and ("tool" in raw or "function" in raw):
            logger.warning(
                f"模型 {gpt.model} 疑似不支持 tools（{str(exc)[:150]}），去工具重试"
            )
            return gpt.client.chat.completions.create(
                **{k: v for k, v in kwargs.items() if k != "tools"}
            )
        raise


def chat(
    task_id: str,
    question: str,
    history: list[dict],
    provider_id: str,
    model_name: str,
    scope: str = "current",
    task_ids: Optional[list] = None,
) -> dict:
    """
    RAG + Tool Calling 问答。
    1. 向量检索初始上下文（scope="all" 时跨全部历史笔记）
    2. 调用 LLM（带 tools）
    3. 如果 LLM 调用了工具，执行工具并将结果返回给 LLM
    4. 循环直到 LLM 给出最终回答

    ``scope``: "current"（默认，只查当前笔记，历史行为）|
    "all"（跨笔记，查 ``task_ids`` 或全部已索引笔记）。
    工具调用（查原文/元信息/全文）始终绑定当前 ``task_id`` 笔记，
    跨笔记内容由初始检索提供。
    """
    vector_store = VectorStoreManager()

    # 1. 检索初始上下文
    if scope == "all":
        chunks = vector_store.query_cross(question, task_ids=task_ids)
    else:
        chunks = vector_store.query(task_id, question, n_results=6)
    context = _build_context(chunks) if chunks else "（未检索到相关内容，请使用工具查询）"
    sources = _build_sources(chunks) if chunks else []

    # 2. 构建消息
    system_msg = SYSTEM_PROMPT.format(context=context)
    messages = [{"role": "system", "content": system_msg}]

    for msg in history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": question})

    # 3. 获取 LLM client
    provider = ProviderService.get_provider_by_id(provider_id)
    if not provider:
        raise ValueError(f"未找到模型供应商: {provider_id}")

    config = ModelConfig(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model_name=model_name,
        provider=provider["type"],
        name=provider["name"],
        api_format=provider.get("api_format") or "chat",
    )
    gpt = GPTFactory.from_config(config)

    logger.info(f"Chat: task_id={task_id}, model={model_name}, scope={scope}")

    # 4. Tool calling 循环（最多 3 轮）
    max_rounds = 3
    for round_i in range(max_rounds):
        response = _call_llm(gpt, messages, use_tools=True)

        msg = response.choices[0].message

        # 没有工具调用，直接返回。tools 只是 LLM 主动深挖当前笔记的手段，
        # 跨笔记证据以初始全局检索为准：工具返回只追加进对话，不污染 sources。
        if not msg.tool_calls:
            return {"answer": msg.content or "", "sources": sources}

        # 处理工具调用
        messages.append(msg)

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"Tool call [{round_i+1}/{max_rounds}]: {fn_name}({fn_args})")

            result = execute_tool(task_id, fn_name, fn_args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # 超过最大轮次，做最后一次不带 tools 的调用
    response = _call_llm(gpt, messages, use_tools=False)

    return {"answer": response.choices[0].message.content or "", "sources": sources}

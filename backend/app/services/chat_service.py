import json
import re
from typing import Optional

from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.services.provider import ProviderService
from app.services.vector_store import VectorStoreManager
from app.services.chat_tools import TOOLS, execute_tool
from app.utils.logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """你是一个视频笔记问答助手。你拥有以下能力：

1. 系统已自动检索了一些相关内容作为初始参考（见下方，每段开头带 [1]、[2]… 编号）
2. 你可以调用工具主动查询更多信息：
   - lookup_transcript: 查询视频原始转录文本（支持按时间、关键词、位置筛选）
   - get_video_info: 获取视频元信息（标题、作者、简介、标签等）
   - get_note_content: 获取完整笔记内容

--- 初始检索内容 ---
{context}
---

回答要求：
- 来自初始检索片段的信息，请在对应句子或段落后标注来源编号，如 [1]；
  一段话依赖多个片段时可并列标注，如 [1][3]
- 只能引用上面已列出的编号，禁止编造未出现的编号；没有检索内容时不要标注
- 工具返回的内容若开头标注了编号，回答用到时同样用该编号标注
- 如果初始检索内容不足以回答问题，请主动调用工具获取更多信息
- 回答关于视频具体原话、细节时，用 lookup_transcript 查询原文
- 回答关于作者、标题等基本信息时，用 get_video_info 查询
- 初始检索可能来自多篇笔记（片段标签带《标题》前缀）：引用跨笔记内容时
  请注明出自哪篇笔记（用《标题》），不要把不同笔记的内容混为一篇
- 请用中文回答，保持简洁准确"""

# 答案正文里的引用标记：[1] ~ [99]
_CITE_RE = re.compile(r"\[(\d{1,2})\]")


def _as_float(value) -> Optional[float]:
    """时间字段容错：恢复/推送链路里可能出现字符串时间戳，格式化前统一转 float，
    转不动返回 None（宁可标签简略也不能让整个问答 500）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_context(chunks: list[dict]) -> str:
    """将检索到的片段拼接为编号上下文。

    每个片段带 ``[n]`` 序号，n 与 ``_build_sources`` 产出的 sources 数组
    下标（1 起）严格一一对应：LLM 按编号在答案里标注引用，前端把答案
    正文里的 ``[n]`` 渲染成可点击角标、把 sources 渲染成编号来源列表。

    跨笔记片段（metadata 带 note_title/task_id）会在标签前加上
    ``《标题》`` 前缀，方便 LLM 在回答中标出来源；单篇旧索引没有
    这些字段，标签与历史行为一致。
    """
    parts = []
    for idx, chunk in enumerate(chunks, start=1):
        meta = chunk.get("metadata", {})
        source_type = meta.get("source_type", "unknown")
        title_prefix = f"《{meta['note_title']}》" if meta.get("note_title") else ""
        if source_type == "meta":
            label = f"{title_prefix}[视频信息]"
        elif source_type == "markdown":
            label = f"{title_prefix}[笔记 - {meta.get('section_title', '')}]"
        else:
            start = _as_float(meta.get("start_time"))
            end = _as_float(meta.get("end_time"))
            if start is None or end is None:
                label = f"{title_prefix}[转录]"
            else:
                label = f"{title_prefix}[转录 - {start:.0f}s~{end:.0f}s]"
        parts.append(f"[{idx}] {label}\n{chunk['text']}")
    return "\n\n".join(parts)


def _build_sources(chunks: list[dict], default_task_id: str = "") -> list[dict]:
    """从检索片段中提取来源信息（含跨笔记的 task_id / note_title）。

    单篇集合（scope=current）的 metadata 没有 task_id，历史上缺省导致
    前端把本篇来源全部判为不可点击（2026-10-07 修复）：这里用 ask 时
    传入的 task_id 盖章补齐跳转字段。note_title 不盖章——本篇来源的
    徽章本来就不带《标题》前缀。
    """
    sources = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source = {
            "text": chunk["text"][:200],
            "source_type": meta.get("source_type", "unknown"),
        }
        task_id = meta.get("task_id") or default_task_id
        if task_id:
            source["task_id"] = task_id
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


def _find_tool_source(sources: list[dict], hint: dict) -> int:
    """同型同区间的工具来源复用已有编号。

    模型经常连环调用同一工具（同参数重试、分页查询），不去重会在来源
    列表堆出重复徽章。返回 1 起编号，无匹配返回 0。
    """
    for i, s in enumerate(sources, start=1):
        if s.get("source_type") != hint.get("source_type"):
            continue
        if s.get("section_title") != hint.get("section_title"):
            continue
        a_st, a_en = s.get("start_time"), s.get("end_time")
        b_st, b_en = hint.get("start_time"), hint.get("end_time")
        try:
            same_time = a_st is None and b_st is None
            if a_st is not None and b_st is not None:
                same_time = (
                    a_en is not None
                    and b_en is not None
                    and abs(float(a_st) - float(b_st)) < 0.5
                    and abs(float(a_en) - float(b_en)) < 0.5
                )
        except (TypeError, ValueError):
            same_time = False
        if same_time:
            return i
    return 0


def _finalize_answer(answer: str, sources: list[dict]) -> list[dict]:
    """把答案里的 [n] 引用标记与 sources 对齐，给每个 source 打 cited 标记。

    合法编号（1..len(sources)）计入 cited；越界编号不剥离、也不渲染成
    角标（前端只链接合法编号）——答案正文可能带代码片段（arr[7] 这类
    数组下标），后端盲删会破坏代码，原文保留最稳。
    答案没有任何合法标记时全部 cited=False：老模型不配合编号时退化为
    原有形态（底部来源列表照常），零回归。
    """
    n = len(sources)
    cited = {
        int(m.group(1))
        for m in _CITE_RE.finditer(answer or "")
        if 1 <= int(m.group(1)) <= n
    }
    for i, source in enumerate(sources, start=1):
        source["cited"] = i in cited
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
    1. 向量检索初始上下文（scope="all" 时跨全部历史笔记），按 [n] 编号
    2. 调用 LLM（带 tools），要求按编号在答案里标注引用
    3. 如果 LLM 调用了工具，执行工具并将结果返回给 LLM；工具证据同样
       编号计入 sources（前端角标可点）
    4. 循环直到 LLM 给出最终回答，最后把答案里的编号与 sources 对齐

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
    sources = _build_sources(chunks, default_task_id=task_id) if chunks else []

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

        # 没有工具调用，最终对齐编号后返回。工具证据此前已按编号并入
        # sources——来源列表覆盖"初始检索 + 工具深挖"的全部引用出处，
        # 不再是只反映初始检索的半截清单。
        if not msg.tool_calls:
            answer = msg.content or ""
            sources = _finalize_answer(answer, sources)
            return {"answer": answer, "sources": sources}

        # 处理工具调用
        messages.append(msg)

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"Tool call [{round_i+1}/{max_rounds}]: {fn_name}({fn_args})")

            result, source_hint = execute_tool(task_id, fn_name, fn_args)

            # 工具证据入来源：编号续接初始检索（重复调用复用已有编号），
            # 工具消息首行告知模型编号，回答引用工具内容时才有号可标。
            if source_hint is not None:
                cite_no = _find_tool_source(sources, source_hint)
                if not cite_no:
                    sources.append({"task_id": task_id, **source_hint})
                    cite_no = len(sources)
                result = (
                    f"（以下工具返回内容的引用编号为 [{cite_no}]，"
                    f"回答中用到该内容时在句末标注 [{cite_no}]）\n{result}"
                )

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # 超过最大轮次，做最后一次不带 tools 的调用
    response = _call_llm(gpt, messages, use_tools=False)
    answer = response.choices[0].message.content or ""
    sources = _finalize_answer(answer, sources)
    return {"answer": answer, "sources": sources}

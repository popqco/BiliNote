import json
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.services.chat_service import chat as chat_service
from app.services.vector_store import NOTE_OUTPUT_DIR, VectorStoreManager
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R

logger = get_logger(__name__)

router = APIRouter()

# 索引状态追踪: task_id -> "indexing" | "indexed" | "failed"
_index_status: dict[str, str] = {}


class IndexRequest(BaseModel):
    task_id: str
    # 前端兜底内容 {markdown, transcript?, audio_meta?}：部分笔记只存在
    # 前端 IndexedDB（后端任务记录/结果文件已被清理），缺源文件时用 pushed
    # 内容先落盘再索引，否则这些笔记的单篇问答是死局（2026-10-04 手机实机）。
    note: Optional[dict] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    task_id: str
    question: str
    history: list[ChatMessage] = []
    provider_id: str
    model_name: str
    # 问答范围："current"（默认，只查当前笔记，历史行为）|
    # "all"（跨全部已索引笔记）| 单个 task_id（限定查那一篇）。
    scope: str = "current"
    task_ids: Optional[list[str]] = None


class SearchRequest(BaseModel):
    # 语义检索原文块（纯向量召回，无 LLM 调用）。外部 harness（MCP）用
    # 它拿原始片段自己推理，不走 chat/ask 的内置问答链路。
    query: str
    # "all"（默认，跨全部已索引笔记）| "current"（只查 task_id 那一篇）|
    # 单个 task_id（限定查那一篇，与 chat/ask 的 scope 约定一致）。
    scope: str = "all"
    task_id: Optional[str] = None
    task_ids: Optional[list[str]] = None
    # 召回条数上限。注意词面重排/距离闸可能让实际返回少于该值（宁缺勿噪）。
    top_k: int = 6


class BackfillRequest(BaseModel):
    task_ids: Optional[list[str]] = None


def _restore_note_file(task_id: str, note: dict) -> None:
    """把前端推回的笔记内容落盘成标准 note_results json（仅缺文件时）。"""
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")
    if os.path.exists(result_path):
        return
    markdown = note.get("markdown") or ""
    if isinstance(markdown, list):
        markdown = (markdown[-1] or {}).get("content", "") if markdown else ""
    if not str(markdown).strip():
        logger.warning(f"兜底内容缺少 markdown，放弃落盘: {task_id}")
        return
    payload = {
        "markdown": markdown,
        "transcript": note.get("transcript") or {},
        "audio_meta": note.get("audio_meta") or {},
    }
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    tmp_path = result_path + ".restore.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, result_path)
    logger.info(f"前端兜底落盘笔记源文件: {task_id}")


def _do_index(task_id: str):
    """后台执行索引任务。"""
    try:
        _index_status[task_id] = "indexing"
        store = VectorStoreManager()
        store.index_task(task_id)
        _index_status[task_id] = "indexed"
        logger.info(f"索引完成: {task_id}")
    except Exception as e:
        _index_status[task_id] = "failed"
        logger.error(f"索引失败: {task_id}, {e}")


@router.post("/chat/index")
def index_task(data: IndexRequest, background_tasks: BackgroundTasks):
    """触发后台索引，立即返回。"""
    if _index_status.get(data.task_id) == "indexing":
        return R.success(msg="正在索引中")

    # 如果已经索引过，直接返回
    try:
        store = VectorStoreManager()
    except Exception as e:
        # 打包缺依赖（如 chromadb rust bindings）时这里就炸：
        # 直接报人话，不要走到后台再变 failed。
        logger.error(f"索引初始化失败: {data.task_id}, {e}")
        return R.error(msg=f"索引初始化失败：{e}", code=500)
    if store.is_indexed(data.task_id):
        _index_status[data.task_id] = "indexed"
        return R.success(msg="已完成索引")

    # 缺源文件时先用前端兜底内容落盘（见 IndexRequest.note 注释）
    if data.note:
        try:
            _restore_note_file(data.task_id, data.note)
        except Exception as e:
            logger.error(f"兜底落盘失败: {data.task_id}, {e}")

    _index_status[data.task_id] = "indexing"
    background_tasks.add_task(_do_index, data.task_id)
    return R.success(msg="开始索引")


@router.get("/chat/status")
def chat_status(task_id: str):
    """返回索引状态：idle / indexing / indexed / failed。"""
    try:
        # 优先检查内存状态
        status = _index_status.get(task_id)
        if status:
            return R.success(data={"status": status, "indexed": status == "indexed"})

        # 内存没有记录，检查持久化
        store = VectorStoreManager()
        indexed = store.is_indexed(task_id)
        if indexed:
            _index_status[task_id] = "indexed"
        return R.success(data={"status": "indexed" if indexed else "idle", "indexed": indexed})
    except Exception as e:
        logger.error(f"查询索引状态失败: {e}")
        return R.success(data={"status": "idle", "indexed": False})


@router.post("/chat/ask")
def ask_question(data: AskRequest):
    """基于笔记内容的 RAG 问答（scope="all" 时跨全部历史笔记）。"""
    try:
        scope = (data.scope or "current").strip().lower()
        task_ids = data.task_ids
        if scope not in ("current", "all"):
            # scope 传单个 task_id 时视为限定查那一篇
            if task_ids is None and scope:
                task_ids = [data.scope]
                scope = "all"
            else:
                return R.error(msg=f"非法 scope: {data.scope}（仅支持 current / all）", code=400)
        history = [{"role": m.role, "content": m.content} for m in data.history]
        result = chat_service(
            task_id=data.task_id,
            question=data.question,
            history=history,
            provider_id=data.provider_id,
            model_name=data.model_name,
            scope=scope,
            task_ids=task_ids,
        )
        return R.success(data=result)
    except ValueError as e:
        return R.error(msg=str(e))
    except Exception as e:
        # 模型调用失败时把“哪个模型 + 哪个供应商 + 上游原话”透给前端，
        # 否则用户只看到“问答失败”，无法区分是 key 没配、余额不足、
        # 模型名不对还是上游 500。注意 502 这类错误专指“App 经 OpenCode
        # Free 本地代理（127.0.0.1:8787）调上游”的返回——ZCode 走的是它
        # 自己的渠道，与这条链路无关，不能互相证明对方好坏。
        logger.error(f"Chat 问答失败: {e}", exc_info=True)
        try:
            from app.services.provider import ProviderService

            prov = ProviderService.get_provider_by_id(data.provider_id)
            pname = (prov or {}).get("name") or data.provider_id
        except Exception:
            pname = data.provider_id
        return R.error(
            msg=f"问答失败（模型 {data.model_name} / 供应商 {pname}）：{str(e)}"
        )


@router.post("/chat/search")
def search_chunks(data: SearchRequest):
    """语义检索笔记原文块（纯 chromadb 向量召回，无 LLM 调用）。

    与 /chat/ask 的分工：ask 是"检索 + 内置模型作答"一条龙；search 只
    检索并原样返回片段（text + 来源/时间戳/小节标题），由调用方（外部
    harness 的模型）自行推理。嵌入是本地模型，检索环节零在线 API 依赖。
    """
    try:
        query = (data.query or "").strip()
        if not query:
            return R.error(msg="query 不能为空", code=400)
        try:
            top_k = max(1, min(int(data.top_k or 6), 30))
        except Exception:
            top_k = 6

        scope = (data.scope or "all").strip().lower()
        if scope in ("current", "all"):
            task_ids = data.task_ids
            if scope == "current":
                if not data.task_id:
                    return R.error(msg="scope=current 时必须提供 task_id", code=400)
                task_ids = [data.task_id]
        elif data.task_ids is None:
            # scope 传单个 task_id 视为限定查那一篇（与 chat/ask 行为一致）
            task_ids = [scope]
        else:
            task_ids = data.task_ids

        store = VectorStoreManager()
        chunks = store.query_cross(query, task_ids=task_ids, top_k=top_k)

        results = []
        for c in chunks:
            meta = c.get("metadata", {}) or {}
            results.append({
                "text": c.get("text", ""),
                "source_type": meta.get("source_type"),
                "task_id": meta.get("task_id"),
                "note_title": meta.get("note_title"),
                "section_title": meta.get("section_title"),
                "start_time": meta.get("start_time"),
                "end_time": meta.get("end_time"),
                "distance": c.get("distance"),
            })
        return R.success(data={"query": query, "count": len(results), "results": results})
    except Exception as e:
        logger.error(f"chat/search 检索失败: {e}", exc_info=True)
        return R.error(msg=f"检索失败：{e}")


@router.get("/chat/indexed")
def indexed_tasks(limit: int = 500):
    """返回全局索引中已建索引的 task_id 列表（供跨笔记范围提示）。

    注意默认 limit=50 是历史值：笔记超过 50 篇时前端拿到的列表不全，
    覆盖数显示会偏小。indexed_task_ids 已改为分页拉全量，上限提到 500。
    """
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 500
    try:
        store = VectorStoreManager()
        return R.success(data={"task_ids": store.indexed_task_ids(limit=limit)})
    except Exception as e:
        logger.error(f"查询全局索引失败: {e}")
        return R.success(data={"task_ids": []})


@router.get("/chat/coverage")
def index_coverage():
    """覆盖率统计：已索引数 / 笔记总数 / 缺失 task_id（供前端提示条展示）。

    total_notes 扫 note_results/*.json（结果文件在即笔记在）；
    indexed 读全局 all_notes 的 task_id 去重。缺失的由前端一键补建。
    """
    try:
        store = VectorStoreManager()
        # 传 500 拿全量：indexed_task_ids 已分页拉取，limit 只做截断保护。
        # 之前 limit=200 配旧的"取 1200 行截断"实现，96 篇只看到 29 个，
        # 补建永远"零增长"（2026-10-04 实测）。
        indexed = set(store.indexed_task_ids(limit=500))
    except Exception as e:
        logger.error(f"查询全局索引失败: {e}")
        indexed = set()
    try:
        all_ids = set(store._local_note_task_ids())
    except Exception:
        all_ids = set()
    missing = sorted(all_ids - indexed)
    return R.success(data={
        "indexed": len(indexed & all_ids),
        "total_notes": len(all_ids),
        "missing": missing,
    })


@router.post("/chat/backfill")
def backfill_global_index(data: BackfillRequest, background_tasks: BackgroundTasks):
    """为缺失全局索引的历史笔记补建索引（后台执行，复用双写 index_task）。"""
    task_ids = data.task_ids

    def _do_backfill(task_ids):
        try:
            store = VectorStoreManager()
            result = store.backfill_global(only_task_ids=task_ids)
            logger.info(f"全局补索引完成: {result}")
        except Exception as e:
            logger.error(f"全局补索引失败: {e}")

    background_tasks.add_task(_do_backfill, task_ids)
    return R.success(msg="开始补建索引")

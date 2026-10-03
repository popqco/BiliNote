from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.services.chat_service import chat as chat_service
from app.services.vector_store import VectorStoreManager
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R

logger = get_logger(__name__)

router = APIRouter()

# 索引状态追踪: task_id -> "indexing" | "indexed" | "failed"
_index_status: dict[str, str] = {}


class IndexRequest(BaseModel):
    task_id: str


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


class BackfillRequest(BaseModel):
    task_ids: Optional[list[str]] = None


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


@router.get("/chat/indexed")
def indexed_tasks(limit: int = 50):
    """返回全局索引中已建索引的 task_id 列表（供跨笔记范围提示）。"""
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 50
    try:
        store = VectorStoreManager()
        return R.success(data={"task_ids": store.indexed_task_ids(limit=limit)})
    except Exception as e:
        logger.error(f"查询全局索引失败: {e}")
        return R.success(data={"task_ids": []})


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

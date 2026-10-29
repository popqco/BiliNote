"""笔记分类管理接口（设置/历史列表共用）。

分类是独立元数据层，不涉及笔记正文与任务状态；归类/取消归类走 assign，
删除分类只断开关联（笔记变回未分类），绝不删笔记。
"""

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.note_categories import NoteCategoryError, note_category_store
from app.utils.response import ResponseWrapper as R

router = APIRouter()


class CreateCategoryRequest(BaseModel):
    name: str


class RenameCategoryRequest(BaseModel):
    id: str
    name: str


class DeleteCategoryRequest(BaseModel):
    id: str


class AssignCategoryRequest(BaseModel):
    task_ids: List[str] = []
    category_id: Optional[str] = None


@router.get("/note_categories")
def list_note_categories():
    return R.success({"categories": note_category_store().list_categories()})


@router.post("/note_categories/create")
def create_note_category(data: CreateCategoryRequest):
    try:
        return R.success(note_category_store().create(data.name), msg="已创建")
    except NoteCategoryError as e:
        return R.error(msg=str(e), code=400)
    except Exception as e:
        return R.error(msg=f"创建分类失败：{e}", code=500)


@router.post("/note_categories/rename")
def rename_note_category(data: RenameCategoryRequest):
    try:
        return R.success(note_category_store().rename(data.id, data.name), msg="已重命名")
    except NoteCategoryError as e:
        return R.error(msg=str(e), code=400)
    except Exception as e:
        return R.error(msg=f"重命名分类失败：{e}", code=500)


@router.post("/note_categories/delete")
def delete_note_category(data: DeleteCategoryRequest):
    try:
        return R.success(note_category_store().delete(data.id), msg="已删除分类")
    except NoteCategoryError as e:
        return R.error(msg=str(e), code=400)
    except Exception as e:
        return R.error(msg=f"删除分类失败：{e}", code=500)


@router.post("/note_categories/assign")
def assign_note_category(data: AssignCategoryRequest):
    try:
        return R.success(note_category_store().assign(data.task_ids, data.category_id))
    except Exception as e:
        return R.error(msg=f"归类失败：{e}", code=500)

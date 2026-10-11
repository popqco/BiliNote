from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from app.services.model import ModelService
from app.utils.response import ResponseWrapper as R
router = APIRouter()
modelService = ModelService()
class CreateModelRequest(BaseModel):
    provider_id: str
    model_name: str
    # 思考强度为模型级属性：'' = 不传参（默认）
    thinking_effort: Optional[str] = ""

# 返回体：模型信息
class ModelItem(BaseModel):
    id: int
    model_name: str

class UpdateModelRequest(BaseModel):
    thinking_effort: Optional[str] = ""
@router.get("/model_list")
def model_list():
    try:
        return R.success(modelService.get_all_models(True),msg="获取模型列表成功")
    except Exception as e:
        return R.error(e)
@router.get("/models/delete/{model_id}")
def delete_model(model_id: int):
    try:
        success = modelService.delete_model_by_id(model_id)
        if success:
            return R.success(msg="模型删除成功")
        else:
            return R.error("模型不存在或删除失败")
    except Exception as e:
        return R.error(f"删除模型失败: {e}")
@router.get("/model_list/{provider_id}")
def model_list(provider_id):

    return R.success(modelService.get_all_models_by_id(provider_id))


@router.post("/models")
def create_model(data: CreateModelRequest):
    success = ModelService.add_new_model(data.provider_id, data.model_name, data.thinking_effort or "")
    if not success:
        return R.error("模型添加失败")
    return R.success(msg="模型添加成功")


@router.post("/models/{model_id}/update")
def update_model(model_id: int, data: UpdateModelRequest):
    """改单个模型的思考强度（不同模型档位不同，故挂在模型而非供应商上）。"""
    try:
        success = modelService.update_model_effort(model_id, data.thinking_effort or "")
        if success:
            return R.success(msg="已更新思考强度")
        return R.error("模型不存在或更新失败")
    except Exception as e:
        return R.error(f"更新思考强度失败: {e}")

@router.get("/model_enable/{provider_id}")
def get_enabled_models_by_provider(provider_id: str):
    try:
        models = modelService.get_enabled_models_by_provider(provider_id)
        return R.success(models, msg="获取启用模型成功")
    except Exception as e:
        return R.error(f"获取启用模型失败: {e}")
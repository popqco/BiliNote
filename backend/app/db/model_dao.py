from app.db.engine import get_db
from app.db.models.models import Model
from app.db.models.providers import Provider


def get_model_by_provider_and_name(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(provider_id=provider_id, model_name=model_name).first()
        if model:
            return {
                "id": model.id,
                "provider_id": model.provider_id,
                "model_name": model.model_name,
                "created_at": model.created_at,
            }
        return None
    finally:
        db.close()


def insert_model(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = Model(provider_id=provider_id, model_name=model_name)
        db.add(model)
        db.commit()
        db.refresh(model)
        return {
            "id": model.id,
            "provider_id": model.provider_id,
            "model_name": model.model_name,
            "created_at": model.created_at,
        }
    finally:
        db.close()


def get_models_by_provider(provider_id: int):
    db = next(get_db())
    try:
        models = db.query(Model).filter_by(provider_id=provider_id).all()
        return [{"id": m.id, "model_name": m.model_name} for m in models]
    finally:
        db.close()


def delete_model(model_id: int):
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(id=model_id).first()
        if model:
            db.delete(model)
            db.commit()
    finally:
        db.close()


def delete_models_by_provider(provider_id) -> int:
    """删除某供应商下全部模型（删供应商时级联清理，避免 models 表留孤儿行）。

    注意：models.provider_id 列是 Integer，而 providers.id 是 String UUID；
    字符串 UUID 在 SQLite 里存不进 Integer 列，旧数据可能混杂两种形态，
    这里两种都匹配删除。返回删除的行数。
    """
    db = next(get_db())
    try:
        rows = db.query(Model).filter_by(provider_id=provider_id).all()
        count = len(rows)
        for m in rows:
            db.delete(m)
        # 兼容历史脏数据：provider_id 存成字符串 UUID 的行
        try:
            str_rows = db.query(Model).filter(Model.provider_id == str(provider_id)).all()
            for m in str_rows:
                if m not in rows:
                    db.delete(m)
                    count += 1
        except Exception:
            pass
        db.commit()
        return count
    finally:
        db.close()


def get_all_models():
    db = next(get_db())
    try:
        # 只查询启用状态供应商的模型
        models = db.query(Model).join(Provider, Model.provider_id == Provider.id).filter(Provider.enabled == 1).all()
        return [
            {"id": m.id, "provider_id": m.provider_id, "model_name": m.model_name}
            for m in models
        ]
    finally:
        db.close()
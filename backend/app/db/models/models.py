from sqlalchemy import Column, Integer, String, DateTime, func, ForeignKey

from app.db.engine import Base


class Model(Base):
    __tablename__ = "models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, nullable=False)
    model_name = Column(String, nullable=False)
    # 思考强度（reasoning effort）：模型级而非供应商级——不同模型的档位不同。
    # ''/None = 不传该参数；low/medium/high/xhigh/max 等原样透传
    thinking_effort = Column(String, nullable=True, server_default="")
    created_at = Column(DateTime, server_default=func.now())
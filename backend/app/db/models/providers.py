from sqlalchemy import Column, String, Integer, DateTime, func
from sqlalchemy.orm import declarative_base

from app.db.engine import Base


class Provider(Base):
    __tablename__ = "providers"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    logo = Column(String, nullable=False)
    type = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    # 供应商 API 协议："chat"（默认，Chat Completions）| "responses"
    # （OpenAI Responses，OpenCode 系网关等只提供该协议）。
    # server_default 保证旧库 ALTER 后已有行为 chat，与历史表现一致。
    api_format = Column(String, nullable=False, server_default="chat", default="chat")
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
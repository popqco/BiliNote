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
    # 自定义请求头（JSON 字符串，如 {"x-opencode-session": "<uuid>"}）。
    # OpenCode Go 等网关要求每会话带稳定会话头（MissingSessionID 400），通用
    # 通道比按 base_url 硬编码干净。存 JSON 串而非 dict：SQLite 列存文本最稳。
    extra_headers = Column(String, nullable=True, server_default="{}")
    # 思考强度（reasoning effort）："" / NULL = 不传该参数（默认，行为不变）；
    # low/medium/high/xhigh/max 等原样透传给上游，不支持的模型由调用端去参重试兜底。
    thinking_effort = Column(String, nullable=True, server_default="")
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
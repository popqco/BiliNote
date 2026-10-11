from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ModelConfig:
    """
    存储每个模型提供商的调用参数信息，用于从数据库读取并动态构建 GPT 调用实例。
    """
    name: str                   # 展示名，如 "GPT-4 Turbo"（用于前端展示）
    provider: str               # 模型提供商，如 "openai"、"qwen"、"deepseek"
    api_key: str                # 调用该模型使用的 API Key
    base_url: str               # 模型 API 接口地址（OpenAI SDK兼容）
    model_name: str             # 实际请求用的模型名称，如 "gpt-4-turbo"
    api_format: Optional[str] = None  # 供应商 API 协议："chat"（默认 Chat Completions）| "responses"（OpenAI Responses，仅部分网关支持）
    # 自定义请求头（JSON 字符串，如 {"x-opencode-session": "<uuid>"}），None/空 = 不附加
    extra_headers: Optional[str] = None
    # 思考强度（reasoning effort）：''/None = 不传该参数；low/medium/high/xhigh/max
    # 等原样透传，不支持的模型由调用端去参重试兜底
    thinking_effort: Optional[str] = None
    created_at: Optional[datetime] = None  # 可选：创建时间（从 SQLite 自动生成）
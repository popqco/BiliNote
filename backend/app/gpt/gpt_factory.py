from openai import OpenAI

from app.gpt.base import GPT
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.gpt.provider.protocol_router import AutoProtocolClient
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig


class GPTFactory:
    @staticmethod
    def from_config(config: ModelConfig) -> GPT:
        client = OpenAICompatibleProvider(
            api_key=config.api_key,
            base_url=config.base_url,
            extra_headers=getattr(config, "extra_headers", None),
        ).get_client
        # 协议路由（2026-10-17）：首选供应商配置的 api_format；同一网关下
        # chat / responses 模型混装、模型挂错供应商时，网关以「协议不匹配」
        # 拒绝就地换协议重发并记住选择，不再整条笔记全灭。
        client = AutoProtocolClient(
            client, preferred=getattr(config, "api_format", None) or "chat"
        )
        return UniversalGPT(
            client=client,
            model=config.model_name,
            thinking_effort=getattr(config, "thinking_effort", None),
        )

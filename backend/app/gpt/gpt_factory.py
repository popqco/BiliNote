from openai import OpenAI

from app.gpt.base import GPT
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.gpt.provider.responses_compat import ResponsesCompatClient
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig


class GPTFactory:
    @staticmethod
    def from_config(config: ModelConfig) -> GPT:
        client = OpenAICompatibleProvider(api_key=config.api_key, base_url=config.base_url).get_client
        if (getattr(config, "api_format", None) or "chat") == "responses":
            # Responses-only 供应商（OpenCode 系网关等）：chat/completions
            # 会 502，包一层协议适配，调用面保持 chat 形态不变。
            client = ResponsesCompatClient(client)
        return UniversalGPT(client=client, model=config.model_name)

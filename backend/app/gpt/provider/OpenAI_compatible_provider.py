import json
from typing import Optional, Union

from app.utils.logger import get_logger
from app.utils.openai_client import build_openai_client

logging= get_logger(__name__)

def parse_extra_headers(extra_headers: Optional[str]) -> Optional[dict]:
    """把供应商配置里的「自定义请求头」JSON 串解析成 dict。

    非法 JSON / 非对象 / 键值非字符串一律降级为 None（不附加任何头）并告警，
    决不能让一个配错的头把整条调用链炸掉。
    """
    raw = str(extra_headers or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as e:
        logging.warning(f"自定义请求头不是合法 JSON，已忽略（{e}）：{raw[:120]}")
        return None
    if not isinstance(data, dict):
        logging.warning(f"自定义请求头必须是 JSON 对象，已忽略：{raw[:120]}")
        return None
    cleaned = {str(k): str(v) for k, v in data.items() if str(k).strip()}
    return cleaned or None


class OpenAICompatibleProvider:
    def __init__(self, api_key: str, base_url: str, model: Union[str, None]=None, extra_headers: Optional[str]=None):
        # build_openai_client：注入全局代理 + 校验 api_key 非空 + 附加自定义请求头
        self.client = build_openai_client(
            api_key, base_url, key_label="模型供应商的 API Key",
            default_headers=parse_extra_headers(extra_headers),
        )
        self.model = model

    @property
    def get_client(self):
        return self.client

    @staticmethod
    def test_connection(api_key: str, base_url: str, model: str, api_format: str = "chat", extra_headers: Optional[str] = None) -> bool:
        """发一条最小化请求验证 key / base_url / model 三方都通。

        为什么不用 client.models.list()：
          - 部分代理 / 自建供应商不实现 /v1/models（如某些 OpenAI 兼容网关）
          - 部分供应商 key 在没有 inference 权限时 /v1/models 仍返回 200
        最终用户跑的就是配置的 api_format 对应调用，按它测最忠实：
        chat → chat.completions.create；responses → responses.create
        （Responses-only 网关如 OpenCode Free 上 chat/completions 会 502，
        用 chat 测会把好端端的供应商报成连不通）。
        开销压到最小：chat max_tokens=1；responses 用 max_output_tokens=16
        （推理模型预算里 reasoning 先扣，给太小程序可能直接空回但不报错）。
        """
        try:
            client = build_openai_client(
                api_key, base_url, key_label="模型供应商的 API Key", timeout=15.0,
                default_headers=parse_extra_headers(extra_headers),
            )
            if (api_format or "chat") == "responses":
                from app.gpt.provider.responses_compat import ResponsesCompatClient

                ResponsesCompatClient(client).chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=16,
                )
            else:
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    temperature=0,
                )
            logging.info(f"连通性测试成功（model={model}, api_format={api_format}）")
            return True
        except Exception as e:
            logging.warning(f"连通性测试失败（model={model}）：{e}")
            return False

"""统一构造 OpenAI 兼容客户端：注入全局代理 + 校验 api_key。

为什么要这一层：
  - 代理：openai SDK 默认只认进程级 HTTP_PROXY 环境变量，桌面端用户在 UI 里
    填的代理需要显式塞进 httpx.Client 才生效。
  - api_key 校验：空 key 会让 httpx 拼出非法 header `Bearer `，抛出
    `httpx.LocalProtocolError: Illegal header value b'Bearer '` 这种天书报错。
    在入口挡掉，给用户「xxx 的 API Key 未配置」这种能看懂的提示。
"""
import os
import urllib.parse
from typing import Optional

from openai import OpenAI

from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _is_loopback_base_url(base_url: Optional[str]) -> bool:
    """base_url 是否指向本机回环地址（本地网关/转换器，如 127.0.0.1:8787）。

    回环地址永远不该走代理：桌面端进程环境里可能带着 HTTP_PROXY（被带代理
    变量的终端拉起时继承而来），绕经系统代理转发到 127.0.0.1 只会引入一层
    有害的转发——2026-10-11 实锤：httpx 经 BeiBeiCore(7892) 访问本机 muse
    网关被代理层直接回 502 空响应，而 curl 走同一代理却正常，全链路总结
    因此全灭（网关日志里压根没有这些请求）。"""
    try:
        host = urllib.parse.urlparse(str(base_url or "")).hostname or ""
    except Exception:
        return False
    return host.lower() in ("127.0.0.1", "localhost", "::1")


def _effective_proxy_url(base_url: Optional[str]) -> Optional[str]:
    """该 base_url 当前应使用的代理 URL；回环地址恒为 None（本地网关直连）。"""
    if _is_loopback_base_url(base_url):
        return None
    return ProxyConfigManager().get_proxy_url()


def build_openai_client(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    key_label: str = "API Key",
    timeout: Optional[float] = None,
    use_system_proxy: bool = False,
) -> OpenAI:
    """构造 OpenAI 客户端。api_key 为空直接抛清晰错误；代理已配置则注入。

    key_label 用于错误提示，例如 "Groq 的 API Key" / "OpenAI 供应商的 API Key"。

    ``use_system_proxy``：应用未配置代理时，默认 trust_env=False 绕系统代理
    直连（防 Clash 对流式 LLM 请求假死）；置 True 则改用 trust_env=True，
    让 httpx 读系统（注册表）代理设置——转写这类一次性大请求在中转站拒绝
    直连（403）时需要这条退路，见 transcriber/groq.py。
    """
    if not api_key or not str(api_key).strip():
        raise ValueError(f"{key_label} 未配置，请先在「设置」里填写后再使用")

    kwargs = {"api_key": str(api_key).strip(), "base_url": base_url}
    # 分形状超时：连接 20s 快速失败（防代理或 CDN 假死把 worker 挂满），读取阶段
    # 默认 180s 空闲上限——流式下分片持续重置计时，见 _shaped_timeout 注释。
    if timeout is None:
        timeout = _shaped_timeout()
    kwargs["timeout"] = timeout

    loopback = _is_loopback_base_url(base_url)
    proxy_url = _effective_proxy_url(base_url)
    import httpx
    if proxy_url:
        kwargs["http_client"] = httpx.Client(proxy=proxy_url, timeout=timeout)
        logger.info(f"OpenAI 客户端走代理: {proxy_url}")
    elif use_system_proxy and not loopback:
        kwargs["http_client"] = httpx.Client(trust_env=True, timeout=timeout)
        logger.info("OpenAI 客户端走系统代理（trust_env=True）")
    else:
        # 关键：httpx trust_env=True 时会经 urllib.getproxies() 读到 Windows
        # 注册表系统代理（Clash 等）。实测 Clash 转发大 LLM 请求会间歇性假死
        # （py-spy 实锤卡在 http_proxy 收响应头）。应用未配置代理时显式
        # trust_env=False 直连，绕开系统代理。
        kwargs["http_client"] = httpx.Client(trust_env=False, timeout=timeout)
        if loopback:
            logger.info("OpenAI 客户端直连（回环地址，本地网关不走代理）")
        else:
            logger.info("OpenAI 客户端直连（绕过系统代理）")

    return OpenAI(**kwargs)


def _shaped_timeout():
    """分形状超时：连接 20s / 写入 120s / 池 30s，读取默认 180s。

    read 指两次网络读取之间的最大间隔，而不是总时长——流式请求下每个 SSE 分片
    都会重置计时器，所以 180s 内没有任何字节基本等于上游真挂了；快速失败换重试
    （或进入降级阶梯用更小的请求）比挂满 600s 的吞吐高得多。
    OPENAI_TIMEOUT_SECONDS 只覆盖读取上限，不再影响连接阶段的快速失败。
    """
    import httpx
    total = os.getenv("OPENAI_TIMEOUT_SECONDS")
    read_timeout = float(total) if total else 180.0
    return httpx.Timeout(connect=20.0, read=read_timeout, write=120.0, pool=30.0)

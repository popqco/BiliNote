"""B 站 API 统一 HTTP Session：强制直连，永不走代理。

为什么存在（2026-10-11 实锤）：BiliNote 启动时会按 config/proxy.json 把代理
写进进程环境变量（为 HuggingFace 下载设计），而 requests 默认 trust_env=True
会吃这些变量。本地代理进程退出后，watchlater/video_meta/字幕等所有 B 站请求
集体撞 ProxyError 10061（每轮自动化连败），但 B 站是国内服务、直连本来就是
通的。B 站域名的调用一律走这里：trust_env=False 同时拒掉环境变量代理与
注册表系统代理，代理的死活不再影响 B 站功能。

LLM/YouTube 等境外目标仍按 proxy.json 配置走代理（见 openai_client /
bilibili_downloader），与本模块无关。
"""
import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def bilibili_session() -> requests.Session:
    """新的直连 Session：UA 预置，trust_env=False（代理免疫）。

    Cookie / Referer 等业务头由调用方按接口自行附加（headers 参数或
    session.headers.update），这里只固定「直连 + UA」这两个不变量。
    """
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
    return s

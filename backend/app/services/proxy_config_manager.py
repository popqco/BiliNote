import json
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


def proxy_reachable(url: str, timeout: float = 0.5) -> bool:
    """TCP 探活：代理的 host:port 现在能不能连上。

    2026-10-11 实测：本地代理进程退出后，env/显式配置里的 127.0.0.1:7892 变成
    「连接被拒」，吃代理的每一类调用（requests trust_env、yt-dlp、httpx 显式
    proxy）全部撞死；而 B 站等直连目标本是通的。探活是所有降级判断的地基。
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class ProxyConfigManager:
    """全局代理配置，存 JSON 文件，支持前端动态修改。

    作用范围：LLM API + 转写 API（Groq 等）+ yt-dlp 视频下载。
    优先级：配置文件里 enabled=true 的 url > 环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。
    这样桌面端/web 用户在设置页填，docker/服务器部署用环境变量兜底。
    """

    def __init__(self, filepath: str = "config/proxy.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: Dict[str, Any]):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_config(self) -> Dict[str, Any]:
        data = self._read()
        return {
            "enabled": bool(data.get("enabled", False)),
            "url": data.get("url", "") or "",
        }

    def update_config(self, enabled: bool, url: Optional[str] = None) -> Dict[str, Any]:
        data = self._read()
        data["enabled"] = bool(enabled)
        if url is not None:
            data["url"] = url.strip()
        self._write(data)
        return self.get_config()

    def get_proxy_url(self) -> Optional[str]:
        """返回当前生效的代理 URL；没有则 None。

        - 配置文件 enabled=true 且 url 非空 → 用配置的 url
        - 否则回退到环境变量（标准的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，大小写都认）
        """
        cfg = self.get_config()
        if cfg["enabled"] and cfg["url"]:
            return cfg["url"]
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            val = os.environ.get(key)
            if val:
                return val
        return None

    def apply_to_env(self) -> Optional[str]:
        """把当前生效的代理 URL 写进进程环境变量，返回生效的 url（无则 None）。

        为什么需要（issue #417）：huggingface_hub / requests 这类库**只认**环境变量
        HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，不读我们 UI 配置文件。whisper 模型用
        snapshot_download 从 HuggingFace 拉取，如果用户只在设置页填了代理，下载根本
        不走代理 —— 就是用户说的「Docker 容器里代理没生效」。在下载前/启动时调用本
        方法，把 UI 配的代理 export 到环境变量，HF 下载就能复用同一个代理。

        大小写别名都写，覆盖不同库的读取习惯。

        2026-10-11 加固：注入前先 TCP 探活，代理进程死了就不注入——否则
        watchlater 这类 trust_env=True 的 requests、yt-dlp 全部撞死代理
        （ProxyError 10061），而直连目标（B 站 API）本是通的。
        """
        url = self.get_proxy_url()
        if not url:
            return None
        if not proxy_reachable(url):
            logger.error(
                f"配置的代理不可达，本次跳过注入环境变量（trust_env 的 requests/yt-dlp 将直连）：{url}"
            )
            return None
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = url
        return url

import json
import re
from pathlib import Path
from typing import Optional, Dict

# 「key=」开头即视为已经带了键名的 Cookie 串
_HAS_KEY_RE = re.compile(r"^\s*[A-Za-z0-9_\-.\[\]]+\s*=")

# 各平台「只填值」时补哪个键名（B 站文档只说要 SESSDATA，用户常常只复制值）
_BARE_VALUE_KEY = {"bilibili": "SESSDATA"}


def normalize_cookie(platform: str, raw: Optional[str]) -> Optional[str]:
    """把用户在「下载配置」里填的内容规整成可直接放进 Cookie 请求头的字符串。

    三种常见填法都要兼容：
    1) 完整 Cookie 串（`buvid3=..; SESSDATA=..; bili_jct=..`）→ 原样返回；
    2) 带键名的单条（`SESSDATA=xxx`）→ 原样返回；
    3) 只填 SESSDATA 的值（`7b78a864%2C...`）→ 补上 `SESSDATA=`。

    第 3 种若不补键名，后端会把这个值当成整条 Cookie 发出去，B 站一律判未登录
    （code=-101，报「账号未登录」）；yt-dlp 的 Netscape cookiefile 也会因为它
    没有 `=` 而解析出 0 条 cookie，等于没登录下载。
    """
    v = str(raw or "").strip()
    if not v:
        return None
    if ";" in v or _HAS_KEY_RE.match(v):
        return v
    key = _BARE_VALUE_KEY.get(platform)
    return f"{key}={v}" if key else v


class CookieConfigManager:
    def __init__(self, filepath: str = "config/downloader.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> Dict[str, Dict[str, str]]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: Dict[str, Dict[str, str]]):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, platform: str) -> Optional[str]:
        data = self._read()
        return normalize_cookie(platform, data.get(platform, {}).get("cookie"))

    def set(self, platform: str, cookie: str):
        data = self._read()
        data[platform] = {"cookie": cookie}
        self._write(data)

    def delete(self, platform: str):
        data = self._read()
        if platform in data:
            del data[platform]
            self._write(data)

    def list_all(self) -> Dict[str, str]:
        data = self._read()
        return {k: v.get("cookie", "") for k, v in data.items()}

    def exists(self, platform: str) -> bool:
        return self.get(platform) is not None
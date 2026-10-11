import json
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

# 「key=」开头即视为已经带了键名的 Cookie 串
_HAS_KEY_RE = re.compile(r"^\s*[A-Za-z0-9_\-.\[\]]+\s*=")

# 各平台「只填值」时补哪个键名（B 站文档只说要 SESSDATA，用户常常只复制值）
_BARE_VALUE_KEY = {"bilibili": "SESSDATA"}


def extract_cookie_value(cookie_str: Optional[str], name: str) -> Optional[str]:
    """从 `k=v; k=v` 形式的 Cookie 串里取指定键的值（没有则 None）。"""
    for part in str(cookie_str or "").split(";"):
        if "=" not in part:
            continue
        k, _, v = part.strip().partition("=")
        if k.strip() == name:
            return v.strip()
    return None


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

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: Dict[str, Dict[str, Any]]):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, platform: str) -> Optional[str]:
        data = self._read()
        return normalize_cookie(platform, data.get(platform, {}).get("cookie"))

    def get_auth(self, platform: str) -> Dict[str, Any]:
        """返回平台的完整登录凭据（cookie 字符串 + 续期元数据）。

        旧结构（只有 cookie 键）天然兼容：缺的键按 None 处理。
        """
        entry = self._read().get(platform) or {}
        return {
            "cookie": normalize_cookie(platform, entry.get("cookie")),
            "refresh_token": entry.get("refresh_token"),
            "updated_at": entry.get("updated_at"),
            "refreshed_at": entry.get("refreshed_at"),
            "checked_at": entry.get("checked_at"),
        }

    def set_auth(
        self,
        platform: str,
        cookie: str,
        refresh_token: Optional[str] = None,
        refreshed_at: Optional[float] = None,
        checked_at: Optional[float] = None,
    ):
        """写入 Cookie 与续期凭据。

        refresh_token 未显式给出时：优先从新 Cookie 串里解析 ac_time_value
        （用户手动粘贴完整 Cookie 也能自动续期）；否则仅当 SESSDATA 没变才
        沿用旧值——换了账号后旧 refresh_token 必然失配（刷新报 86095），不能留。
        """
        data = self._read()
        old = data.get(platform) or {}
        if refresh_token is None:
            refresh_token = extract_cookie_value(cookie, "ac_time_value")
        if refresh_token is None:
            old_sess = extract_cookie_value(str(old.get("cookie") or ""), "SESSDATA")
            new_sess = extract_cookie_value(cookie, "SESSDATA")
            if old_sess and old_sess == new_sess:
                refresh_token = old.get("refresh_token")
        data[platform] = {
            "cookie": cookie,
            "refresh_token": refresh_token,
            "updated_at": time.time(),
            "refreshed_at": refreshed_at if refreshed_at is not None else old.get("refreshed_at"),
            "checked_at": checked_at if checked_at is not None else old.get("checked_at"),
        }
        self._write(data)

    def mark_checked(self, platform: str):
        """记录最近一次「Cookie 是否需要刷新」的探测时间（用于节流，不改动凭据）。"""
        data = self._read()
        entry = data.get(platform)
        if not entry:
            return
        entry["checked_at"] = time.time()
        self._write(data)

    def set(self, platform: str, cookie: str):
        # 兼容旧签名：手动保存走 set_auth（保留可用的续期凭据）
        self.set_auth(platform, cookie)

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
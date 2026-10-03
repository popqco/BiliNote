import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional


class PairingManager:
    """Viewer 配对 token 管理：存 JSON 文件，支持前端动态查看/重生成。

    作用范围：远端 Viewer 直连 Worker 的 /api 鉴权。
    本机回环（localhost / 127.0.0.1）免鉴，保证 All-in-One 桌面端与扩展不受影响。
    token 经环境变量 WORKER_PAIRING_TOKEN 覆盖（容器/CI 场景）。
    allow_remote（远控总开关，票 5）：False 时远端 Viewer 只能走"任务白名单"
    （提交/查询/历史/封面），其余 /api 一律 403；本机回环不受影响。
    """

    ENV_KEY = "WORKER_PAIRING_TOKEN"

    def __init__(self, filepath: str = "config/pairing.json"):
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

    def _env_token(self) -> Optional[str]:
        val = os.getenv(self.ENV_KEY, "")
        return val.strip() or None

    def get_token(self) -> str:
        """返回当前生效 token；文件缺失时生成并持久化。"""
        env_token = self._env_token()
        if env_token:
            return env_token
        data = self._read()
        token = (data.get("token") or "").strip()
        if token:
            return token
        token = secrets.token_urlsafe(32)
        self._write({"token": token})
        return token

    def has_custom_token(self) -> bool:
        """是否已有用户侧 token（环境变量或文件），用于判断是否首次生成。"""
        if self._env_token():
            return True
        return bool((self._read().get("token") or "").strip())

    def regenerate(self) -> str:
        """重新生成 token 并持久化；环境变量覆盖时拒绝（需改环境变量本身）。"""
        if self._env_token():
            raise RuntimeError("当前 token 由环境变量 WORKER_PAIRING_TOKEN 提供，请直接修改该变量")
        token = secrets.token_urlsafe(32)
        self._write({"token": token})
        return token

    def verify(self, candidate: Optional[str]) -> bool:
        if not candidate:
            return False
        return secrets.compare_digest(candidate.strip(), self.get_token())

    # ---- 远控总开关（票 5） ----

    def get_allow_remote(self) -> bool:
        """远端 Viewer 是否可改全局配置；缺省 True（历史行为：配对即全开）。"""
        data = self._read()
        # 文件里没写过该键 → 默认允许（老用户升级不断连）。
        if "allow_remote" not in data:
            return True
        return bool(data.get("allow_remote"))

    def set_allow_remote(self, allowed: bool) -> bool:
        """设置远控开关。token 缺失时先生成（保证文件结构完整），返回生效值。"""
        data = self._read()
        if not (data.get("token") or "").strip():
            data["token"] = secrets.token_urlsafe(32)
        data["allow_remote"] = bool(allowed)
        self._write(data)
        return bool(allowed)

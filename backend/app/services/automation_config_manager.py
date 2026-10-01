"""自动化（稍后再看定期检查 + 通知）配置管理。

落盘 config/automation.json（与 proxy.json/downloader.json 同目录约定，
打包环境下即 D:\\Program Files\\BiliNote\\config\\automation.json）。

说明（见 docs/adr/0004）：
- 调度双轨：应用内线程 + Windows 计划任务 CLI，共用本配置；
- gen 段是「自动化生成配置」：独立于首页表单当前选择的模型/风格/视频理解开关，
  无人值守时结果可预期（用户决策 Q10）。
"""
import json
from pathlib import Path
from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 120,
    "mode": "all",           # all=全部未总结 | window=仅最近 window_days 天新增
    "window_days": 7,
    "max_per_round": 5,      # 每轮最多新提交任务数（防一次性灌爆队列）
    "gen": {
        "provider_id": "",
        "model_name": "",
        "style": "minimal",
        "format": ["toc", "summary", "link"],
        "quality": "medium",
        "video_understanding": False,
        "video_interval": 6,
        "grid_size": [2, 2],
        "extras": "",
    },
    "notify": {
        "wxpusher": {"enabled": False, "app_token": "", "uids": ""},
        "smtp": {
            "enabled": False,
            "host": "",
            "port": 465,
            "username": "",
            "password": "",
            "to": "",
        },
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class AutomationConfigManager:
    def __init__(self, filepath: str = "config/automation.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(DEFAULT_CONFIG)

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_config(self) -> Dict[str, Any]:
        """读取配置，并以默认值补齐缺失字段（老配置文件向前兼容）。"""
        return _deep_merge(DEFAULT_CONFIG, self._read())

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = _deep_merge(self.get_config(), patch or {})
        self._write(merged)
        return merged

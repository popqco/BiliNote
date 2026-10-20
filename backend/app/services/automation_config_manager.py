"""自动化（稍后再看定期检查 + 通知）配置管理。

落盘 config/automation.json（与 proxy.json/downloader.json 同目录约定，
打包环境下即 D:\\Program Files\\BiliNote\\config\\automation.json）。

说明（见 docs/adr/0004）：
- 调度双轨：应用内线程 + Windows 计划任务 CLI，共用本配置；
- gen 段是「自动化生成配置」：独立于首页表单当前选择的模型/风格/视频理解开关，
  无人值守时结果可预期（用户决策 Q10）。
"""
from typing import Any, Dict

from app.config.json_store import JsonFileStore
from app.paths import CONFIG_DIR
from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 120,
    "mode": "all",           # all=全部未总结 | window=仅最近 window_days 天新增
    "window_days": 7,
    "max_per_round": 5,      # 每轮最多新提交任务数（防一次性灌爆队列）
    "retry_cooldown_minutes": 30,  # 同一视频失败后的冷却窗（失败不落库，靠它防每轮重放）
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
        "min_interval_minutes": 30,  # 两次通知的最小间隔（0 = 每轮都发），与扫描频率解耦
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


class AutomationConfigManager:
    def __init__(self, filepath: str = str(CONFIG_DIR / "automation.json")):
        self._store = JsonFileStore(filepath, DEFAULT_CONFIG)
        self._store.ensure_exists()

    def get_config(self) -> Dict[str, Any]:
        """读取配置，并以默认值补齐缺失字段（老配置文件向前兼容）。"""
        return self._store.read_merged()

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._store.update(patch)

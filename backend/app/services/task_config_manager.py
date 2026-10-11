"""生成性能与预算配置管理。

落盘 config/task_config.json（与 automation.json/proxy.json 同目录约定），
控制三类全局参数：
- 并行：max_workers（同时生成的笔记数，改动热生效）；
- 视频理解预算：frame_budget（单任务抽帧总数上限）/ image_budget（喂给模型的
  拼图数量上限），见 note._adapt_frame_budget；
- 总结：summarize_batch_concurrency（分批总结时的并发批次数）。

取值优先级：配置文件 > 环境变量 > 内置默认。默认 4800/1000 是 2026-10-11
用户定版的激进档（旧档 2400/500）；环境变量保留作部署侧逃生口——一旦在 UI
保存过一次，四个键都会落盘，环境变量随即失效。
"""
import json
import os
from pathlib import Path
from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG: Dict[str, int] = {
    "max_workers": 2,                  # 同时生成的笔记数（TASK_MAX_WORKERS 可在文件外覆盖）
    "frame_budget": 4800,              # 单任务抽帧总数上限（旧档 2400）
    "image_budget": 1000,              # 喂给模型的拼图数量上限（旧档 500）
    "summarize_batch_concurrency": 2,  # 分批总结的并发批次数
}

# key -> (环境变量名, 下限, 上限)
_LIMITS: Dict[str, tuple] = {
    "max_workers": ("TASK_MAX_WORKERS", 1, 16),
    "frame_budget": ("FRAME_BUDGET", 100, 200_000),
    "image_budget": ("IMAGE_BUDGET", 10, 50_000),
    "summarize_batch_concurrency": ("SUMMARIZE_BATCH_CONCURRENCY", 1, 8),
}


def _clamp(key: str, value: Any) -> int:
    lo, hi = _LIMITS[key][1], _LIMITS[key][2]
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_CONFIG[key]
    return max(lo, min(hi, n))


class TaskConfigManager:
    def __init__(self, filepath: str = "config/task_config.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            # 文件不存在/损坏 = 从未在 UI 保存过，交给环境变量/默认值
            return {}

    def get(self, key: str) -> int:
        """单个参数的生效值：文件 > 环境变量 > 默认，越界自动夹取。"""
        if key not in DEFAULT_CONFIG:
            raise KeyError(key)
        raw = self._read().get(key)
        if raw is not None:
            return _clamp(key, raw)
        env_raw = os.getenv(_LIMITS[key][0])
        if env_raw:
            return _clamp(key, env_raw)
        return DEFAULT_CONFIG[key]

    def get_config(self) -> Dict[str, int]:
        return {k: self.get(k) for k in DEFAULT_CONFIG}

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, int]:
        """只接受已知键，逐键夹取后落盘；返回保存后的生效配置。"""
        current = self._read()
        for k in DEFAULT_CONFIG:
            if k in (patch or {}):
                current[k] = _clamp(k, patch[k])
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        logger.info(f"生成性能与预算配置已更新: {self.get_config()}")
        return self.get_config()

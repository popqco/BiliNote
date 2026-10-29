"""自动化忽略名单（删除墓碑）：用户手动删掉的视频不再被「稍后再看」自动生成。

2026-10-29 用户反馈「删了又生成」：自动化去重只有两条证据——``video_tasks``
表里有这个视频、或 ``note_results`` 里有它的未完成任务；而删除笔记会把这两条
证据一起抹掉（purge_task 删文件 + 条件删库行），稍后再看列表本身又不会把视频
移出——下一轮它看起来和「从未生成过」完全一样，必然重新提交。

这个名单就是第三份证据：删除笔记时落一条「已删除」，自动化轮前查一次；用户
主动重新提交同一视频 = 改变主意，提交入口自动解除。只影响自动化，手动/MCP
提交永不被拦。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from app.config import JsonFileStore
from app.paths import CONFIG_DIR
from app.utils.logger import get_logger
from app.utils.url_parser import normalize_video_id

logger = get_logger(__name__)

MAX_ITEMS = 500  # 稍后再看最多 100 条，500 条足够覆盖用户删除过的历史视频

DEFAULT_DATA: Dict[str, object] = {"items": {}}

# 路径是模块级常量，测试通过 monkeypatch 指到 tmp 目录（不要写进真实 config）
IGNORE_FILE = CONFIG_DIR / "automation_ignore.json"


def ignore_key(video_id: str, platform: str = "bilibili") -> str:
    """名单键：``platform:归一化video_id``（多 P 的 ``_pN`` 后缀先剥掉）。"""
    plat = (platform or "bilibili").strip().lower()
    vid = normalize_video_id(video_id, platform) or video_id
    return f"{plat}:{vid}"


class AutomationIgnoreStore:
    """``{items: {key: {video_id, platform, title, task_id, ts, reason}}}``"""

    def __init__(self, filepath: Optional[Path] = None):
        self._store = JsonFileStore(filepath or IGNORE_FILE, DEFAULT_DATA)
        self._store.ensure_exists()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 读
    def _read_items(self) -> Dict[str, dict]:
        data = self._store.read() or {}
        items = data.get("items")
        if not isinstance(items, dict):
            return {}
        return {k: v for k, v in items.items() if isinstance(v, dict)}

    def keys(self) -> set:
        """一轮检查前的快照（避免一百个视频各读一次文件）。"""
        return set(self._read_items().keys())

    def contains(self, video_id: str, platform: str = "bilibili") -> bool:
        if not video_id:
            return False
        return ignore_key(video_id, platform) in self._read_items()

    def items(self) -> List[dict]:
        """按加入时间倒序的列表（设置页展示用）。"""
        rows = [{"key": k, **v} for k, v in self._read_items().items()]
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows

    def version(self) -> str:
        """mtime_ns:size 指纹，供 /tasks/recent 判断名单是否变化。"""
        try:
            st = self._store.path.stat()
        except OSError:
            return ""
        return f"{st.st_mtime_ns}:{st.st_size}"

    # ------------------------------------------------------------------ 写
    def add(
        self,
        video_id: str,
        platform: str = "bilibili",
        title: str = "",
        task_id: str = "",
        reason: str = "manual_delete",
    ) -> Optional[dict]:
        """加入名单；video_id 为空时直接返回 None（local 任务没有视频身份）。"""
        if not video_id:
            return None
        key = ignore_key(video_id, platform)
        entry = {
            "video_id": video_id,
            "platform": (platform or "bilibili").strip().lower(),
            "title": str(title or "")[:200],
            "task_id": str(task_id or ""),
            "ts": time.time(),
            "reason": reason,
        }
        with self._lock:
            items = self._read_items()
            items[key] = entry
            if len(items) > MAX_ITEMS:
                # 超出上限丢掉最旧的（稍后再看最多 100 条，正常永远到不了）
                ordered = sorted(items.items(), key=lambda kv: float(kv[1].get("ts") or 0))
                items = dict(ordered[-MAX_ITEMS:])
            self._store.write({"items": items})
        logger.info(f"已加入自动化忽略名单：{key}（{entry['title'] or '-'}）")
        return {"key": key, **entry}

    def remove(self, video_id: str, platform: str = "bilibili") -> bool:
        if not video_id:
            return False
        key = ignore_key(video_id, platform)
        with self._lock:
            items = self._read_items()
            if key not in items:
                return False  # 未命中不写盘：手动提交每次都会调到这里
            items.pop(key, None)
            self._store.write({"items": items})
        logger.info(f"已从自动化忽略名单移除：{key}")
        return True

    def clear(self) -> int:
        with self._lock:
            items = self._read_items()
            if not items:
                return 0
            self._store.write({"items": {}})
        logger.info(f"已清空自动化忽略名单：{len(items)} 条")
        return len(items)


_stores: Dict[str, AutomationIgnoreStore] = {}
_stores_lock = threading.Lock()


def automation_ignore_store(filepath: Optional[Path] = None) -> AutomationIgnoreStore:
    """按路径缓存的单例工厂（同一进程内共享一把写锁）。"""
    path = Path(filepath or IGNORE_FILE)
    try:
        cache_key = str(path.resolve())
    except OSError:
        cache_key = str(path)
    with _stores_lock:
        store = _stores.get(cache_key)
        if store is None:
            store = AutomationIgnoreStore(path)
            _stores[cache_key] = store
    return store

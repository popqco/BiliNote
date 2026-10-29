"""笔记分类：用户自定义分类 + 每篇笔记的归属（单选）。

存储是独立的 JSON 元数据层（``config/note_categories.json``），不改 SQLite
schema、不改 ``note_results`` 文件格式、不改状态 JSON——存量笔记零迁移，
没有赋值的一律视为「未分类」。分类列表与赋值同文件，写入走 JsonFileStore
（临时文件 + os.replace 原子替换）并加线程锁。

与任务生命周期只在一处相交：删除笔记时 ``remove_task`` 清掉它的赋值，
避免孤儿映射无限堆积。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app.config import JsonFileStore
from app.paths import CONFIG_DIR
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_CATEGORIES = 50
MAX_NAME_LENGTH = 30

# 固定调色板：按创建顺序轮转取色，前端用色点/徽标区分
PALETTE = [
    "#ef4444",
    "#f97316",
    "#eab308",
    "#22c55e",
    "#14b8a6",
    "#3b82f6",
    "#8b5cf6",
    "#ec4899",
    "#64748b",
]

DEFAULT_DATA: Dict[str, object] = {"categories": [], "assignments": {}}

CATEGORY_FILE = CONFIG_DIR / "note_categories.json"


class NoteCategoryError(ValueError):
    """分类操作的业务错误（重名/空名/不存在），由路由层转成 400。"""


class NoteCategoryStore:
    def __init__(self, filepath: Optional[Path] = None):
        self._store = JsonFileStore(filepath or CATEGORY_FILE, DEFAULT_DATA)
        self._store.ensure_exists()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 读
    def _read(self) -> dict:
        data = self._store.read() or {}
        cats = data.get("categories")
        assigns = data.get("assignments")
        return {
            "categories": cats if isinstance(cats, list) else [],
            "assignments": assigns if isinstance(assigns, dict) else {},
        }

    def list_categories(self) -> List[dict]:
        return [c for c in self._read()["categories"] if isinstance(c, dict) and c.get("id")]

    def assignments(self) -> Dict[str, str]:
        return {str(k): str(v) for k, v in self._read()["assignments"].items() if v}

    def version(self) -> str:
        try:
            st = self._store.path.stat()
        except OSError:
            return ""
        return f"{st.st_mtime_ns}:{st.st_size}"

    def _require(self, cat_id: str) -> dict:
        for c in self.list_categories():
            if str(c.get("id")) == str(cat_id):
                return c
        raise NoteCategoryError("分类不存在（可能已被删除）")

    def _name_taken(self, name: str, exclude_id: Optional[str] = None) -> bool:
        for c in self.list_categories():
            if str(c.get("id")) == str(exclude_id):
                continue
            if str(c.get("name") or "").strip() == name:
                return True
        return False

    # ------------------------------------------------------------------ 写
    def create(self, name: str) -> dict:
        name = str(name or "").strip()
        if not name:
            raise NoteCategoryError("分类名称不能为空")
        if len(name) > MAX_NAME_LENGTH:
            raise NoteCategoryError(f"分类名称最长 {MAX_NAME_LENGTH} 个字符")
        with self._lock:
            data = self._read()
            cats = [c for c in data["categories"] if isinstance(c, dict)]
            if len(cats) >= MAX_CATEGORIES:
                raise NoteCategoryError(f"分类最多 {MAX_CATEGORIES} 个")
            if self._name_taken(name):
                raise NoteCategoryError(f"分类「{name}」已存在")
            used = {str(c.get("id")) for c in cats}
            cat_id = uuid.uuid4().hex[:8]
            while cat_id in used:
                cat_id = uuid.uuid4().hex[:8]
            cat = {
                "id": cat_id,
                "name": name,
                "color": PALETTE[len(cats) % len(PALETTE)],
                "created_at": int(time.time()),
            }
            cats.append(cat)
            self._store.write({"categories": cats, "assignments": data["assignments"]})
        logger.info(f"新建笔记分类：{name} ({cat_id})")
        return cat

    def rename(self, cat_id: str, name: str) -> dict:
        name = str(name or "").strip()
        if not name:
            raise NoteCategoryError("分类名称不能为空")
        if len(name) > MAX_NAME_LENGTH:
            raise NoteCategoryError(f"分类名称最长 {MAX_NAME_LENGTH} 个字符")
        with self._lock:
            data = self._read()
            cats = [c for c in data["categories"] if isinstance(c, dict)]
            target = None
            for c in cats:
                if str(c.get("id")) == str(cat_id):
                    target = c
                    break
            if target is None:
                raise NoteCategoryError("分类不存在（可能已被删除）")
            if self._name_taken(name, exclude_id=str(cat_id)):
                raise NoteCategoryError(f"分类「{name}」已存在")
            target["name"] = name
            self._store.write({"categories": cats, "assignments": data["assignments"]})
        logger.info(f"重命名笔记分类：{cat_id} → {name}")
        return target

    def delete(self, cat_id: str) -> dict:
        """删除分类：分类下的笔记变回未分类（笔记本身不动）。"""
        with self._lock:
            data = self._read()
            cats = [c for c in data["categories"] if isinstance(c, dict)]
            kept = [c for c in cats if str(c.get("id")) != str(cat_id)]
            if len(kept) == len(cats):
                raise NoteCategoryError("分类不存在（可能已被删除）")
            assigns = {
                k: v for k, v in data["assignments"].items() if str(v) != str(cat_id)
            }
            removed = len(data["assignments"]) - len(assigns)
            self._store.write({"categories": kept, "assignments": assigns})
        logger.info(f"删除笔记分类：{cat_id}，{removed} 篇笔记变回未分类")
        return {"removed_assignments": removed}

    def assign(self, task_ids: List[str], category_id: Optional[str]) -> dict:
        """批量归类；``category_id=None`` 表示设为未分类。

        分类不存在时按 no-op 处理（自动化默认分类指向已删除的分类、多端并发
        删除等场景都不该让调用方报错）。
        """
        ids = [str(t) for t in (task_ids or []) if str(t or "").strip()]
        if not ids:
            return {"updated": 0}
        with self._lock:
            data = self._read()
            assigns = dict(data["assignments"])
            if category_id is not None:
                cat_id = str(category_id)
                if not any(str(c.get("id")) == cat_id for c in data["categories"] if isinstance(c, dict)):
                    logger.warning(f"归类到不存在的分类 {cat_id}，已忽略（{len(ids)} 个任务）")
                    return {"updated": 0}
                for tid in ids:
                    assigns[tid] = cat_id
            else:
                for tid in ids:
                    assigns.pop(tid, None)
            changed = assigns != data["assignments"]
            if changed:
                self._store.write({"categories": data["categories"], "assignments": assigns})
        return {"updated": len(ids) if changed else 0}

    def remove_task(self, task_id: str) -> bool:
        """任务（笔记）被删除时清掉它的赋值。"""
        tid = str(task_id or "").strip()
        if not tid:
            return False
        with self._lock:
            data = self._read()
            assigns = dict(data["assignments"])
            if tid not in assigns:
                return False
            assigns.pop(tid, None)
            self._store.write({"categories": data["categories"], "assignments": assigns})
        return True


_stores: Dict[str, NoteCategoryStore] = {}
_stores_lock = threading.Lock()


def note_category_store(filepath: Optional[Path] = None) -> NoteCategoryStore:
    """按路径缓存的单例工厂（同一进程内共享一把写锁）。"""
    path = Path(filepath or CATEGORY_FILE)
    try:
        cache_key = str(path.resolve())
    except OSError:
        cache_key = str(path)
    with _stores_lock:
        store = _stores.get(cache_key)
        if store is None:
            store = NoteCategoryStore(path)
            _stores[cache_key] = store
    return store

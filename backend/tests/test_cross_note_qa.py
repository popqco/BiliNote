"""跨笔记知识库问答回归测试。

约定遵循 tests/ 现有风格：unittest + importlib 按文件加载被测模块，
避免 import app.* 触发 FastAPI/重型依赖链。
vector_store 依赖 chromadb（共享 venv 已装），用临时目录隔离
VECTOR_DB_DIR / NOTE_OUTPUT_DIR，不污染开发库。
"""

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_TMP = None


def _load_vector_store(tmpdir: str):
    """在隔离环境目录下加载 vector_store 模块（单例复用，保证同一 Chroma 实例）。"""
    global TEST_TMP
    TEST_TMP = tmpdir
    os.environ["VECTOR_DB_DIR"] = os.path.join(tmpdir, "vector_db")
    os.environ["NOTE_OUTPUT_DIR"] = os.path.join(tmpdir, "note_results")
    os.makedirs(os.environ["NOTE_OUTPUT_DIR"], exist_ok=True)

    # 给被测模块一个轻量 logger，避免 import app.utils.logger 拽入重型链
    import types

    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    utils_pkg = types.ModuleType("app.utils")
    utils_pkg.__path__ = []
    logger_mod = types.ModuleType("app.utils.logger")

    import logging

    def get_logger(name):
        return logging.getLogger(name)

    logger_mod.get_logger = get_logger
    sys.modules["app"] = app_pkg
    sys.modules["app.utils"] = utils_pkg
    sys.modules["app.utils.logger"] = logger_mod

    module_name = "cross_qa_vector_store"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "app" / "services" / "vector_store.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("vector_store module spec not found")
    vs = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = vs
    spec.loader.exec_module(vs)
    return vs


def _write_note(note_dir: str, task_id: str, title: str, keyword: str, body: str):
    """写一份最小可索引笔记：标题进 meta chunk，正文进 markdown。

    注意 markdown 标题与正文关键词刻意错开（如标题“量子纠缠科普”、
    正文关键词“回锅肉郫县豆瓣”不许出现在同一篇），避免 embedding
    把标题词和正文词混成同一语义，干扰跨查召回测试。
    """
    md = f"## 本篇核心观点\n\n{body} {keyword} {keyword} {keyword}\n"
    note = {
        "markdown": md,
        "transcript": {"segments": []},
        "audio_meta": {"title": title, "platform": "bilibili", "duration": 60},
    }
    with open(os.path.join(note_dir, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False)


class TestCrossNoteQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmpdir = tempfile.mkdtemp(prefix="cross_qa_")
        cls._tmpdir = tmpdir
        cls.vs = _load_vector_store(tmpdir)
        cls.note_dir = os.environ["NOTE_OUTPUT_DIR"]
        # 两篇主题完全不同的笔记：A=量子物理，B=川菜烹饪
        _write_note(
            cls.note_dir,
            "task-aaa-physics",
            "量子纠缠科普",
            "量子纠缠叠加态",
            "本篇讲解微观粒子之间的关联现象与实验验证。",
        )
        _write_note(
            cls.note_dir,
            "task-bbb-cooking",
            "川菜回锅肉做法",
            "回锅肉郫县豆瓣",
            "本篇讲解家常菜的选材切配与火候控制。",
        )
        cls.store = cls.vs.VectorStoreManager()
        cls.store.index_task("task-aaa-physics")
        cls.store.index_task("task-bbb-cooking")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_single_scope_unchanged(self):
        """单篇模式行为不变：只查当前笔记，不串到另一篇。"""
        chunks = self.store.query("task-aaa-physics", "这道菜怎么做")
        self.assertTrue(len(chunks) > 0)
        for c in chunks:
            # 单篇 collection 的 metadata 没有 task_id（历史行为）
            self.assertNotIn("task_id", c.get("metadata", {}))

    def test_cross_recall_other_note(self):
        """跨查能召回另一篇的内容并标出来源（task_id + note_title）。"""
        chunks = self.store.query_cross("回锅肉怎么做")
        task_ids = {c["metadata"].get("task_id") for c in chunks}
        self.assertIn("task-bbb-cooking", task_ids)
        cooking = [c for c in chunks if c["metadata"].get("task_id") == "task-bbb-cooking"]
        self.assertTrue(cooking)
        self.assertEqual(cooking[0]["metadata"].get("note_title"), "川菜回锅肉做法")

    def test_cross_limit_to_task_ids(self):
        """task_ids 限定只查给定笔记，查不到范围外的内容。"""
        chunks = self.store.query_cross("回锅肉", task_ids=["task-bbb-cooking"])
        self.assertTrue(chunks)
        task_ids = {c["metadata"].get("task_id") for c in chunks}
        self.assertTrue(task_ids <= {"task-bbb-cooking"})
        self.assertNotIn("task-aaa-physics", task_ids)

    def test_cross_only_keeps_relevant_notes(self):
        """高度相关时不掺无关笔记：量子问题只召回物理笔记。

        （margin 相对截断：与最佳候选差距大的弱相关片段被丢弃，
        不再像旧按篇配额那样每篇硬塞来源。）
        """
        chunks = self.store.query_cross("量子纠缠叠加态实验验证")
        self.assertTrue(chunks)
        task_ids = {c["metadata"].get("task_id") for c in chunks}
        self.assertIn("task-aaa-physics", task_ids)
        self.assertNotIn("task-bbb-cooking", task_ids)

    def test_fallback_path_pure(self):
        """全局路径与重建都失败时，回退路径也不掺无关笔记。

        （2026-10-03 用户实拍 bug：旧回退按篇配额召回后轮询合并，
        问 A 笔记的问题引用里混进 B 笔记片段。修复后回退与全局路径
        共用同一套词面/距离筛选，噪音在合并后被剔掉。）
        """
        store = self.store
        store._query_global = lambda collection, q, where: None
        store._rebuild_global_from_per_note = lambda: False
        try:
            chunks = store.query_cross("量子纠缠叠加态实验验证")
        finally:
            del store._query_global
            del store._rebuild_global_from_per_note
        self.assertTrue(chunks)
        task_ids = {c["metadata"].get("task_id") for c in chunks}
        self.assertNotIn("task-bbb-cooking", task_ids)
        for c in chunks:
            self.assertTrue(c["metadata"].get("note_title"))

    def test_query_self_heals_broken_global(self):
        """全局集合查询瞬态失败（HNSW 错）时：重建一次→重查成功，
        不需要降级到回退路径，结果仍带跨篇来源标记。"""
        store = self.store
        real_query = store._query_global
        calls = {"n": 0}

        def flaky(collection, query_text, where):
            calls["n"] += 1
            if calls["n"] == 1:
                # _query_global 内部吞异常返回 None，这里模拟坏段
                raise RuntimeError("Error creating hnsw index (simulated)")
            return real_query(collection, query_text, where)

        store._query_global = flaky
        try:
            chunks = store.query_cross("量子纠缠叠加态实验验证")
        finally:
            del store._query_global
        self.assertEqual(calls["n"], 2)  # 第一次失败 + 重建后重查
        self.assertTrue(chunks)
        for c in chunks:
            self.assertEqual(c["metadata"].get("task_id"), "task-aaa-physics")

    def test_query_with_task_ids_routes_to_global(self):
        """query(task_ids=...) 走全局索引并带来源标记。"""
        chunks = self.store.query(
            "task-aaa-physics", "回锅肉", task_ids=["task-bbb-cooking"]
        )
        self.assertTrue(chunks)
        self.assertEqual(chunks[0]["metadata"].get("task_id"), "task-bbb-cooking")

    def test_backfill_skips_indexed(self):
        """已建全局索引的笔记补索引时跳过，不重复。"""
        result = self.store.backfill_global()
        self.assertEqual(result["indexed"], [])
        self.assertIn("task-aaa-physics", result["skipped"])
        self.assertIn("task-bbb-cooking", result["skipped"])
        self.assertEqual(result["failed"], [])

    def test_backfill_picks_up_missing(self):
        """新增笔记文件后补索引能捡回来。"""
        _write_note(
            self.note_dir,
            "task-ccc-new",
            "阳台种菜指南",
            "阳台种菜有机肥",
            "本篇讲解城市阳台种植叶菜的容器与光照。",
        )
        result = self.store.backfill_global(only_task_ids=["task-ccc-new"])
        self.assertEqual(result["indexed"], ["task-ccc-new"])
        chunks = self.store.query_cross("阳台种菜", task_ids=["task-ccc-new"])
        self.assertTrue(chunks)
        self.assertEqual(chunks[0]["metadata"].get("note_title"), "阳台种菜指南")

    def test_delete_index_cleans_global(self):
        """删除索引时全局记录一并清理，跨查不再召回（最后执行）。"""
        self.store.delete_index("task-ccc-new")
        self.assertFalse(self.store.is_indexed_global("task-ccc-new"))
        self.assertFalse(self.store.is_indexed("task-ccc-new"))
        chunks = self.store.query_cross("阳台种菜")
        task_ids = {c["metadata"].get("task_id") for c in chunks}
        self.assertNotIn("task-ccc-new", task_ids)


if __name__ == "__main__":
    unittest.main()

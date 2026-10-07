"""/api/chat/search 语义检索端点测试（MCP 原文检索用）。

约定遵循 tests/ 现有风格：unittest + TestClient 走真实路由；
VECTOR_DB_DIR / NOTE_OUTPUT_DIR 指到临时目录，不污染开发库。
vector_store / chat_tools 在模块顶层读环境变量，所以这里在设完 env 后
按依赖顺序 reload 相关模块，保证隔离生效。
"""

import importlib
import json
import os
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMPDIR = tempfile.mkdtemp(prefix="chat_search_")

os.environ["VECTOR_DB_DIR"] = os.path.join(TMPDIR, "vector_db")
os.environ["NOTE_OUTPUT_DIR"] = os.path.join(TMPDIR, "note_results")
os.makedirs(os.environ["NOTE_OUTPUT_DIR"], exist_ok=True)

# 按依赖顺序 reload：让模块顶层的 env 读取拿到临时目录
import app.services.vector_store as vector_store_mod

importlib.reload(vector_store_mod)
import app.services.chat_tools as chat_tools_mod

importlib.reload(chat_tools_mod)
import app.services.chat_service as chat_service_mod

importlib.reload(chat_service_mod)
import app.routers.chat as chat_router_mod

importlib.reload(chat_router_mod)

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_note(note_dir: str, task_id: str, title: str, keyword: str, body: str):
    """写一份最小可索引笔记（与 test_cross_note_qa 同款：标题与正文关键词错开）。"""
    md = f"## 本篇核心观点\n\n{body} {keyword} {keyword} {keyword}\n"
    note = {
        "markdown": md,
        "transcript": {"segments": []},
        "audio_meta": {"title": title, "platform": "bilibili", "duration": 60},
    }
    with open(os.path.join(note_dir, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False)


class TestChatSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.note_dir = os.environ["NOTE_OUTPUT_DIR"]
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
        cls.store = vector_store_mod.VectorStoreManager()
        cls.store.index_task("task-aaa-physics")
        cls.store.index_task("task-bbb-cooking")

        app = FastAPI()
        app.include_router(chat_router_mod.router, prefix="/api")
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def _search(self, payload):
        resp = self.client.post("/api/chat/search", json=payload)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_search_all_scope_recalls_right_note(self):
        """scope=all：跨笔记检索命中正确笔记并带来源标记。"""
        body = self._search({"query": "回锅肉怎么做"})
        self.assertEqual(body["code"], 0, body.get("msg"))
        results = body["data"]["results"]
        self.assertTrue(results)
        task_ids = {r["task_id"] for r in results}
        self.assertIn("task-bbb-cooking", task_ids)
        self.assertNotIn("task-aaa-physics", task_ids)
        top = results[0]
        self.assertEqual(top["note_title"], "川菜回锅肉做法")
        self.assertTrue(top["text"])
        self.assertIn("source_type", top)

    def test_search_scope_current(self):
        """scope=current + task_id：只返回该篇的片段。"""
        body = self._search(
            {"query": "量子纠缠叠加态实验验证", "scope": "current", "task_id": "task-aaa-physics"}
        )
        self.assertEqual(body["code"], 0, body.get("msg"))
        results = body["data"]["results"]
        self.assertTrue(results)
        for r in results:
            self.assertEqual(r["task_id"], "task-aaa-physics")

    def test_search_task_ids_and_top_k(self):
        """task_ids 限定范围 + top_k 截断条数。"""
        body = self._search(
            {
                "query": "量子纠缠叠加态实验验证",
                "task_ids": ["task-aaa-physics"],
                "top_k": 2,
            }
        )
        self.assertEqual(body["code"], 0, body.get("msg"))
        results = body["data"]["results"]
        self.assertTrue(results)
        self.assertLessEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["task_id"], "task-aaa-physics")

    def test_search_single_task_id_scope_string(self):
        """scope 传单个 task_id（字符串）：与 chat/ask 约定一致，限定查那一篇。"""
        body = self._search({"query": "回锅肉", "scope": "task-bbb-cooking"})
        self.assertEqual(body["code"], 0, body.get("msg"))
        for r in body["data"]["results"]:
            self.assertEqual(r["task_id"], "task-bbb-cooking")

    def test_search_current_requires_task_id(self):
        """scope=current 缺 task_id → 400 信封错误。"""
        body = self._search({"query": "随便", "scope": "current"})
        self.assertNotEqual(body["code"], 0)
        self.assertIn("task_id", body["msg"])

    def test_search_rejects_empty_query(self):
        body = self._search({"query": "   "})
        self.assertNotEqual(body["code"], 0)

    def test_query_cross_top_k_param(self):
        """vector_store.query_cross 的 top_k 参数生效（默认仍是 CROSS_TOP_K）。"""
        chunks = self.store.query_cross("量子纠缠叠加态实验验证", top_k=1)
        self.assertLessEqual(len(chunks), 1)
        chunks_default = self.store.query_cross("量子纠缠叠加态实验验证")
        self.assertLessEqual(
            len(chunks_default), vector_store_mod.CROSS_TOP_K
        )

    def test_search_path_in_remote_whitelist(self):
        """远程白名单包含 /api/chat/search（远控关闭时已配对 Viewer/远程 MCP 仍可检索）。"""
        from app.middleware.pairing_auth import _is_remote_task_path

        self.assertTrue(_is_remote_task_path("/api/chat/search"))


if __name__ == "__main__":
    unittest.main()

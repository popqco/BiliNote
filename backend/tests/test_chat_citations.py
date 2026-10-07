"""问答引用（行内编号角标）机制回归测试。

覆盖 2026-10-07 引用重构的核心链路：
- _build_context 编号化（与 sources 顺序一一对应、字符串时间戳容错）
- _build_sources 单篇模式 task_id 盖章
- _finalize_answer 答案 [n] 标记 ↔ sources 的 cited 对齐
- _find_tool_source 工具来源去重
- execute_tool 三工具的 source_hint
- _select_cross_top 距离闸 base 修复（词面命中候选为基准）
- query_cross 全局集合缺失时走重建/回退（不再静默返空）
- _clean_section_title 「开头」兜底

约定遵循 tests/ 现有风格：unittest + importlib 按文件加载被测模块，
重依赖（gpt_factory / provider / model_config）用桩模块顶替，
vector_store / chat_tools 加载真实模块（chromadb 共享 venv 已装），
用临时目录隔离 VECTOR_DB_DIR / NOTE_OUTPUT_DIR。
"""

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(rel_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / rel_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"module spec not found: {rel_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _setup_environment():
    """隔离环境 + 轻量桩模块。

    返回 (tmpdir, chat_service, chat_tools, vector_store, sys.modules 备份)。
    备份记录所有被本测试覆盖/新增的 sys.modules 条目，tearDownModule 用它
    精确还原，后续测试（test_chat_search 等真实 app 测试）不受残留影响。
    """
    tmpdir = tempfile.mkdtemp(prefix="chat_citations_")
    os.environ["VECTOR_DB_DIR"] = os.path.join(tmpdir, "vector_db")
    os.environ["NOTE_OUTPUT_DIR"] = os.path.join(tmpdir, "note_results")
    os.makedirs(os.environ["NOTE_OUTPUT_DIR"], exist_ok=True)

    backup: dict = {}

    def _set_modules(mapping: dict):
        for name, mod in mapping.items():
            backup[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = mod

    # 轻量 logger，避免 import app.utils.logger 拽入重型链
    import logging

    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    pkgs = {"app": app_pkg}
    for name in ("app.utils", "app.gpt", "app.models", "app.services"):
        pkg = types.ModuleType(name)
        pkg.__path__ = []
        pkgs[name] = pkg
    logger_mod = types.ModuleType("app.utils.logger")
    logger_mod.get_logger = logging.getLogger
    pkgs["app.utils.logger"] = logger_mod

    # 真实 vector_store / chat_tools（私有名加载；同时顶到 app.services.*
    # 供 chat_service 的 from-import 绑定）
    vs = _load_module("app/services/vector_store.py", "citations_vector_store")
    pkgs["app.services.vector_store"] = vs
    ct = _load_module("app/services/chat_tools.py", "citations_chat_tools")
    pkgs["app.services.chat_tools"] = ct

    # 重依赖桩：本文件只测纯函数与工具层，不真调 LLM
    gpt_stub = types.ModuleType("app.gpt.gpt_factory")
    gpt_stub.GPTFactory = object
    pkgs["app.gpt.gpt_factory"] = gpt_stub
    mc_stub = types.ModuleType("app.models.model_config")
    mc_stub.ModelConfig = object
    pkgs["app.models.model_config"] = mc_stub
    pv_stub = types.ModuleType("app.services.provider")
    pv_stub.ProviderService = object
    pkgs["app.services.provider"] = pv_stub

    _set_modules(pkgs)

    cs = _load_module("app/services/chat_service.py", "citations_chat_service")
    return tmpdir, cs, ct, vs, backup


_TMPDIR = None
chat_service = None
chat_tools = None
vector_store = None
_ENV_BACKUP = None
_SYS_MODULES_BACKUP = None
_MISSING = object()


def setUpModule():
    """执行期（而非收集期）才装桩：模块导入期污染 sys.modules 会把
    之后收集的 test_chat_search 等真实 app 测试炸掉（app 包被换成空桩）。

    执行期污染也不行：test_chat_search 在收集期已按它自己的临时目录
    reload 过模块常量，我改了 os.environ 不恢复的话它会「笔记写 A 目录、
    索引读 B 目录」。所以退出时 env 与 sys.modules 全量恢复。
    """
    global _TMPDIR, chat_service, chat_tools, vector_store, _ENV_BACKUP, _SYS_MODULES_BACKUP
    if chat_service is not None:
        return
    _ENV_BACKUP = {
        k: os.environ.pop(k) if k in os.environ else None
        for k in ("VECTOR_DB_DIR", "NOTE_OUTPUT_DIR")
    }
    _TMPDIR, chat_service, chat_tools, vector_store, _SYS_MODULES_BACKUP = _setup_environment()


def tearDownModule():
    global _ENV_BACKUP, _SYS_MODULES_BACKUP
    if _ENV_BACKUP is not None:
        for key, value in _ENV_BACKUP.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _ENV_BACKUP = None
    if _SYS_MODULES_BACKUP is not None:
        for name, mod in _SYS_MODULES_BACKUP.items():
            if mod is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        _SYS_MODULES_BACKUP = None
    if _TMPDIR:
        shutil.rmtree(_TMPDIR, ignore_errors=True)


def _chunk(meta: dict, text: str = "正文内容") -> dict:
    return {"text": text, "metadata": meta}


class TestBuildContext(unittest.TestCase):
    def test_numbering_matches_chunks_order(self):
        chunks = [
            _chunk({"source_type": "meta"}),
            _chunk({"source_type": "markdown", "section_title": "核心观点"}),
            _chunk({"source_type": "transcript", "start_time": 12.0, "end_time": 45.0}),
        ]
        ctx = chat_service._build_context(chunks)
        i1, i2, i3 = ctx.find("[1]"), ctx.find("[2]"), ctx.find("[3]")
        self.assertGreaterEqual(i1, 0)
        self.assertLess(i1, i2)
        self.assertLess(i2, i3)
        self.assertIn("[1] [视频信息]", ctx)
        self.assertIn("[2] [笔记 - 核心观点]", ctx)
        self.assertIn("[3] [转录 - 12s~45s]", ctx)

    def test_cross_note_title_prefix(self):
        chunks = [_chunk({"source_type": "markdown", "section_title": "A", "note_title": "标题"})]
        ctx = chat_service._build_context(chunks)
        self.assertIn("[1] 《标题》[笔记 - A]", ctx)

    def test_transcript_bad_time_no_crash(self):
        """字符串/缺失时间戳不再把整个问答炸成 500。"""
        chunks = [_chunk({"source_type": "transcript", "start_time": "abc", "end_time": None})]
        ctx = chat_service._build_context(chunks)
        self.assertIn("[1] [转录]", ctx)

    def test_numeric_string_time_still_formats(self):
        chunks = [_chunk({"source_type": "transcript", "start_time": "12.5", "end_time": "45.9"})]
        ctx = chat_service._build_context(chunks)
        self.assertIn("[转录 - 12s~46s]", ctx)


class TestBuildSources(unittest.TestCase):
    def test_current_scope_stamped_with_ask_task_id(self):
        """单篇集合 metadata 无 task_id：用 ask 时的 task_id 盖章，
        前端本篇来源徽章才可点击（2026-10-07 修复）。"""
        chunks = [_chunk({"source_type": "markdown", "section_title": "A"})]
        sources = chat_service._build_sources(chunks, default_task_id="task-1")
        self.assertEqual(sources[0]["task_id"], "task-1")

    def test_cross_scope_keeps_own_task_id(self):
        chunks = [_chunk({"source_type": "markdown", "section_title": "A", "task_id": "other"})]
        sources = chat_service._build_sources(chunks, default_task_id="task-1")
        self.assertEqual(sources[0]["task_id"], "other")


class TestFinalizeAnswer(unittest.TestCase):
    def test_valid_markers_flag_cited(self):
        sources = [{"source_type": "meta"}, {"source_type": "markdown"}, {"source_type": "transcript"}]
        out = chat_service._finalize_answer("结论 [1]，另见 [3] 与越界的 [9]", sources)
        self.assertEqual([s["cited"] for s in out], [True, False, True])

    def test_answer_text_not_modified(self):
        """答案正文不剥离任何标记（可能含代码下标 arr[7]），只打 cited 标。"""
        sources = [{"source_type": "meta"}]
        answer = "数组访问 arr[7] 与引用 [1] 与编造 [5]"
        chat_service._finalize_answer(answer, sources)
        self.assertEqual(answer, "数组访问 arr[7] 与引用 [1] 与编造 [5]")

    def test_no_markers_all_uncited(self):
        """老模型不配合编号时全部 cited=False，退化为原有形态。"""
        sources = [{"source_type": "meta"}, {"source_type": "meta"}]
        out = chat_service._finalize_answer("没有任何标记的回答", sources)
        self.assertEqual([s["cited"] for s in out], [False, False])


class TestFindToolSource(unittest.TestCase):
    def test_duplicate_window_reused(self):
        sources = [{"source_type": "meta"}]
        hint = {"source_type": "transcript", "start_time": 10.0, "end_time": 60.0}
        sources.append({"source_type": "transcript", "start_time": 10.0, "end_time": 60.0})
        self.assertEqual(chat_service._find_tool_source(sources, hint), 2)

    def test_different_window_new_index(self):
        sources = [{"source_type": "transcript", "start_time": 10.0, "end_time": 60.0}]
        hint = {"source_type": "transcript", "start_time": 100.0, "end_time": 150.0}
        self.assertEqual(chat_service._find_tool_source(sources, hint), 0)

    def test_markdown_whole_note_matches_only_whole_note(self):
        """get_note_content（无 section）只和同为整篇的来源复用编号。"""
        sources = [{"source_type": "markdown", "section_title": "章节A"}]
        hint = {"source_type": "markdown"}
        self.assertEqual(chat_service._find_tool_source(sources, hint), 0)
        sources.append({"source_type": "markdown"})
        self.assertEqual(chat_service._find_tool_source(sources, hint), 2)


class TestExecuteToolHints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.note = {
            "markdown": "## 章节\n\n内容内容内容内容。",
            "transcript": {
                "segments": [
                    {"start": 5.0, "end": 9.0, "text": "甲"},
                    {"start": 10.0, "end": 20.0, "text": "乙"},
                    {"start": 30.0, "end": 65.0, "text": "丙"},
                ]
            },
            "audio_meta": {"title": "测试视频", "platform": "bilibili", "duration": 70},
        }
        path = os.path.join(os.environ["NOTE_OUTPUT_DIR"], "task-tool.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cls.note, f, ensure_ascii=False)

    def test_lookup_transcript_hint_spans_filtered(self):
        result, hint = chat_tools.execute_tool(
            "task-tool", "lookup_transcript", {"start_time": 10, "end_time": 60}
        )
        data = json.loads(result)
        self.assertEqual(data["returned"], 2)
        self.assertEqual(hint["source_type"], "transcript")
        self.assertEqual(hint["start_time"], 10.0)
        self.assertEqual(hint["end_time"], 65.0)

    def test_lookup_transcript_empty_result_no_hint(self):
        result, hint = chat_tools.execute_tool(
            "task-tool", "lookup_transcript", {"keyword": "不存在的词"}
        )
        self.assertEqual(json.loads(result)["returned"], 0)
        self.assertIsNone(hint)

    def test_get_video_info_hint_meta(self):
        result, hint = chat_tools.execute_tool("task-tool", "get_video_info", {})
        self.assertIn("测试视频", result)
        self.assertEqual(hint, {"source_type": "meta"})

    def test_get_note_content_hint_markdown(self):
        result, hint = chat_tools.execute_tool("task-tool", "get_note_content", {})
        self.assertIn("章节", result)
        self.assertEqual(hint, {"source_type": "markdown"})

    def test_missing_note_no_hint(self):
        result, hint = chat_tools.execute_tool("task-missing", "get_video_info", {})
        self.assertIn("error", result)
        self.assertIsNone(hint)


class TestSelectCrossTop(unittest.TestCase):
    def test_distance_gate_base_uses_lex_hits(self):
        """距离闸 base 必须取“词面命中候选”的最佳距离。

        复现旧 bug：无关候选 A 距离最近（0.30），旧实现用它当 base，
        词面命中 B(0.60)/C(0.75) 全被 0.25 宽松闸卡掉、只剩保底单条；
        修复后 base=B 的 0.60，B、C 都保留，A 仍被词面闸剔除。
        """
        query = "猛玛极影价格是多少"
        candidates = [
            {"text": "香水的前调是茉莉与檀香", "distance": 0.30},
            {"text": "猛玛极影7 ultra 官方价格 8999 元全套配件", "distance": 0.60},
            {"text": "猛玛极影的套装价格对比", "distance": 0.75},
        ]
        kept = vector_store._select_cross_top(query, candidates, top_k=6)
        self.assertEqual(len(kept), 2)
        self.assertIn("8999", kept[0]["text"])
        self.assertIn("套装价格", kept[1]["text"])
        # 无关候选无论距离多近都不能混进来
        self.assertFalse(any("香水" in c["text"] for c in kept))

    def test_pervasive_question_fallback_unchanged(self):
        """泛问（无词面命中）仍走距离 + CROSS_MARGIN 截断。"""
        candidates = [
            {"text": "甲乙丙丁", "distance": 0.10},
            {"text": "戊己庚辛", "distance": 0.15},
            {"text": "壬癸子丑", "distance": 0.50},
        ]
        kept = vector_store._select_cross_top("随便聊聊", candidates, top_k=6)
        self.assertEqual(len(kept), 2)


class TestQueryCrossMissingGlobal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = vector_store.VectorStoreManager()

    def test_missing_global_falls_back(self):
        """全局集合不存在（老安装从未建索引）时走重建→回退，不再静默返空。"""
        store = self.store

        class _BoomClient:
            def get_collection(self, name):
                raise RuntimeError("collection does not exist")

        sentinel = [{"text": "兜底", "metadata": {"source_type": "markdown"}, "distance": 0.5}]
        orig_client, orig_rebuild, orig_fb = (
            store._client,
            store._rebuild_global_from_per_note,
            store._query_cross_fallback,
        )
        store._client = _BoomClient()
        store._rebuild_global_from_per_note = lambda: True
        store._query_cross_fallback = lambda q, t, k: sentinel
        try:
            chunks = store.query_cross("随便问点什么")
        finally:
            store._client, store._rebuild_global_from_per_note, store._query_cross_fallback = (
                orig_client,
                orig_rebuild,
                orig_fb,
            )
        self.assertEqual(chunks, sentinel)

    def test_rebuild_failure_still_falls_back(self):
        store = self.store
        sentinel = [{"text": "兜底2", "metadata": {}, "distance": 0.5}]
        orig_client, orig_rebuild, orig_fb = (
            store._client,
            store._rebuild_global_from_per_note,
            store._query_cross_fallback,
        )
        store._client = type("B", (), {"get_collection": lambda self, n: (_ for _ in ()).throw(RuntimeError("x"))})()
        store._rebuild_global_from_per_note = lambda: False
        store._query_cross_fallback = lambda q, t, k: sentinel
        try:
            chunks = store.query_cross("再问一句")
        finally:
            store._client, store._rebuild_global_from_per_note, store._query_cross_fallback = (
                orig_client,
                orig_rebuild,
                orig_fb,
            )
        self.assertEqual(chunks, sentinel)


class TestCleanSectionTitle(unittest.TestCase):
    def test_empty_after_clean_returns_kaeitou(self):
        self.assertEqual(vector_store._clean_section_title("原片（04:00）*"), "开头")

    def test_normal_title_stripped_of_suffix(self):
        self.assertEqual(
            vector_store._clean_section_title("专业剧组价值 [原片 @ 04:00](https://x)*"),
            "专业剧组价值",
        )


class TestLexicalTerms(unittest.TestCase):
    def test_brand_term_extracted_generic_dropped(self):
        """品牌 2 字锚进关键串；英文词保持整词；“价格”泛词不作锚。"""
        terms = vector_store._lexical_terms("猛玛极影7 Ultra 的价格是多少")
        self.assertIn("猛玛", terms)
        self.assertIn("Ultra", terms)
        self.assertNotIn("价格", terms)

    def test_long_cjk_run_expanded_to_windows(self):
        """长中文串滑窗展开：潘通问题能产出 潘通/色卡 锚点。"""
        terms = vector_store._lexical_terms("潘通色全套色卡多少钱")
        self.assertIn("潘通", terms)
        self.assertIn("色卡", terms)

    def test_exact_section_title_extracted(self):
        terms = vector_store._lexical_terms("专业剧组与租赁商价值")
        self.assertTrue(any("专业剧组" in t for t in terms))

    def test_vague_question_no_long_anchor(self):
        """泛问句只产出无害碎片，不产生长锚点。"""
        terms = vector_store._lexical_terms("总结一下这个视频")
        self.assertTrue(all(len(t) <= 2 for t in terms))
        self.assertNotIn("视频", terms)
        self.assertNotIn("总结", terms)


class _FakeGlobalCollection:
    """模拟全局集合：query 返回语义候选，get(where_document=...) 返回词面候选。"""

    def __init__(self, semantic, lexical):
        self._semantic = semantic
        self._lexical = lexical

    def query(self, query_texts, n_results, where=None):
        return {
            "ids": [[c["id"] for c in self._semantic]],
            "documents": [[c["text"] for c in self._semantic]],
            "metadatas": [[c["metadata"] for c in self._semantic]],
            "distances": [[c["distance"] for c in self._semantic]],
        }

    def get(self, where=None, where_document=None, include=None, limit=None):
        term = where_document["$contains"]
        matched = [c for c in self._lexical if term in c["text"]]
        n = limit or 12
        return {
            "ids": [c["id"] for c in matched[:n]],
            "documents": [c["text"] for c in matched[:n]],
            "metadatas": [c["metadata"] for c in matched[:n]],
        }


class TestLexicalRecall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = vector_store.VectorStoreManager()

    def _with_fake_client(self, fake):
        store = self.store
        orig_client = store._client
        store._client = type("C", (), {"get_collection": lambda self, name: fake})()
        return orig_client

    def test_lexical_hit_enters_context_after_semantic(self):
        """语义召回只有 intro 噪音时，含关键串的定价块经词面通道进上下文，
        且排在同档语义候选之后（引用编号靠后但至少在场）。"""
        store = self.store
        sem_intro = {
            "id": "note_1",
            "text": "来源链接 BV123 # 猛玛极影7 Ultra 评测",
            "metadata": {"source_type": "markdown", "section_title": "intro"},
            "distance": 0.28,
        }
        sem_noise = {
            "id": "other_1",
            "text": "香水的前调是茉莉与檀香",
            "metadata": {"source_type": "markdown", "section_title": "intro"},
            "distance": 0.30,
        }
        lex_price = {
            "id": "note_7",
            "text": "## 5. 专业剧组与租赁商价值 8999 元的定价决定了极影7 Ultra 的目标用户",
            "metadata": {"source_type": "markdown", "section_title": "5. 专业剧组与租赁商价值"},
            "distance": None,
        }
        fake = _FakeGlobalCollection(semantic=[sem_intro, sem_noise], lexical=[lex_price])
        orig_client = self._with_fake_client(fake)
        try:
            kept = store.query_cross("猛玛极影7 Ultra 的价格是多少")
        finally:
            store._client = orig_client
        texts = [c["text"] for c in kept]
        # 噪音候选被词面闸剔除，定价块在场且排在语义候选之后
        self.assertFalse(any("香水" in t for t in texts))
        self.assertTrue(any("8999" in t for t in texts))
        self.assertEqual(texts[0], sem_intro["text"])

    def test_lexical_dedupe_with_semantic(self):
        """同一块既被语义召回又被词面召回时只保留一份。"""
        store = self.store
        both = {
            "id": "note_1",
            "text": "猛玛极影7 Ultra 评测 开箱",
            "metadata": {"source_type": "markdown", "section_title": "intro"},
            "distance": 0.25,
        }
        fake = _FakeGlobalCollection(semantic=[both], lexical=[both])
        orig_client = self._with_fake_client(fake)
        try:
            kept = store.query_cross("猛玛极影7 Ultra")
        finally:
            store._client = orig_client
        self.assertEqual(len(kept), 1)


class TestSelectCrossTopLexicalFill(unittest.TestCase):
    def test_none_distance_fills_after_semantic(self):
        """distance=None 的词面候选通过词面闸、排在语义候选之后。"""
        sem = {"text": "猛玛极影7 Ultra 评测横评", "distance": 0.28}
        noise = {"text": "香水的前调是茉莉", "distance": 0.30}
        lex = {"text": "猛玛极影7 Ultra 定价 8999 元", "distance": None}
        kept = vector_store._select_cross_top(
            "猛玛极影7 Ultra 的价格是多少", [noise, sem, lex], top_k=6
        )
        self.assertEqual(kept[0]["text"], sem["text"])
        self.assertIn(lex["text"], [c["text"] for c in kept])
        self.assertFalse(any("香水" in c["text"] for c in kept))

    def test_pervasive_question_lexical_sorts_last(self):
        """泛问路径：词面候选不再按 0 距离抢到最前，而是补位在语义之后。"""
        sem = {"text": "甲乙丙丁戊己", "distance": 0.10}
        lex = {"text": "甲乙都是关键词载体", "distance": None}
        kept = vector_store._select_cross_top("甲乙关系如何", [lex, sem], top_k=6)
        self.assertEqual(kept[0]["text"], sem["text"])


if __name__ == "__main__":
    unittest.main()

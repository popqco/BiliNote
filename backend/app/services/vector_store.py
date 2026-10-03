import json
import math
import os
import re
from typing import Optional

import chromadb
from chromadb.config import Settings

from app.utils.logger import get_logger

logger = get_logger(__name__)

NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", "note_results")
VECTOR_DB_DIR = os.getenv("VECTOR_DB_DIR", "vector_db")

# 同一进程内复用 PersistentClient：之前每次请求 new 一个 client，
# 多实例并发读写同一 sqlite 会间歇性报
# "HNSW segment reader: Nothing found on disk"，问答直接变 0 来源。
# 复用单例后 contention 消失；即使仍抛错，query_cross 也会回退到单篇集合。
_SHARED_CLIENTS: dict[str, object] = {}


def _clean_section_title(title: str) -> str:
    """清洗章节标题，去掉可点击跳转后缀，保证来源 badge 与正文 heading 一致。

    笔记 markdown 的 H2/H3 常带“原片（04:00）* / [原片 @ 04:00](url)*”跳转
    后缀：来源 badge 显示的是清洗后的短标题，前端点击后按短标题模糊匹配
    heading 才能命中；不清洗则 badge 文本含 URL/括号变体，匹配失败。
    """
    title = re.sub(r'\s*\[原片\s*@\s*[\d:]+\s*\]\([^)]*\)\*?', '', title).strip()
    # 一个标题可能挂多个原片后缀（如“…原片（07:39）* 原片（08:00）*”），
    # 中间还夹着分隔星号；先把“ * ”分隔符折叠再循环清后缀。
    title = re.sub(r'\s*\*\s*', ' ', title).strip()
    while True:
        cleaned = re.sub(r'\s*原片[（(][\d:]+[）)]\*?', '', title).strip()
        if cleaned == title:
            break
        title = cleaned
    title = title.rstrip('*').strip()
    return title or "intro"


def _chunk_markdown(markdown: str) -> list[dict]:
    """按 H2/H3 标题拆分 markdown 为语义块。"""
    sections = re.split(r'(?=^#{2,3}\s)', markdown, flags=re.MULTILINE)
    chunks = []
    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue
        heading_match = re.match(r'^(#{2,3})\s+(.+)', section)
        raw_title = heading_match.group(2).strip() if heading_match else "intro"
        # section_title 存清洗后的短标题（无原片后缀），正文 chunk 保留原文
        title = _clean_section_title(raw_title.splitlines()[0])
        chunks.append({
            "text": section,
            "metadata": {"source_type": "markdown", "section_title": title},
        })
    return chunks


def _chunk_transcript(segments: list[dict], window_size: int = 15, overlap: int = 3) -> list[dict]:
    """将转录 segments 按滑动窗口分组。"""
    if not segments:
        return []
    chunks = []
    step = max(window_size - overlap, 1)
    for i in range(0, len(segments), step):
        window = segments[i:i + window_size]
        if not window:
            break
        text = "\n".join(
            f"[{seg.get('start', 0):.0f}s] {seg.get('text', '')}" for seg in window
        )
        chunks.append({
            "text": text,
            "metadata": {
                "source_type": "transcript",
                "start_time": window[0].get("start", 0),
                "end_time": window[-1].get("end", 0),
            },
        })
    return chunks


def _build_meta_chunk(audio_meta: dict) -> list[dict]:
    """将视频元信息（标题、作者、描述、标签等）构建为可检索的 chunk。"""
    if not audio_meta:
        return []

    raw = audio_meta.get("raw_info", {}) or {}
    parts = []

    title = audio_meta.get("title") or raw.get("title", "")
    if title:
        parts.append(f"视频标题：{title}")

    uploader = raw.get("uploader", "")
    if uploader:
        parts.append(f"视频作者/UP主：{uploader}")

    desc = raw.get("description", "")
    if desc:
        parts.append(f"视频简介：{desc[:500]}")

    tags = raw.get("tags", [])
    if tags and isinstance(tags, list):
        parts.append(f"标签：{', '.join(str(t) for t in tags[:20])}")

    duration = audio_meta.get("duration", 0)
    if duration:
        m, s = divmod(int(duration), 60)
        parts.append(f"视频时长：{m}分{s}秒")

    platform = audio_meta.get("platform", "")
    if platform:
        parts.append(f"平台：{platform}")

    url = raw.get("webpage_url", "")
    if url:
        parts.append(f"链接：{url}")

    if not parts:
        return []

    return [{
        "text": "\n".join(parts),
        "metadata": {"source_type": "meta"},
    }]


# 价格/费用类同义词：检索前统一成“价格”，否则 embedding 容易把
# “售价/定价/多少钱”问法与“价格”正文错开，无关笔记趁机挤进 Top-K。
_PRICE_SYNONYMS = ["售价", "定价", "报价", "费用", "多少钱", "价位", "价钱"]


def _normalize_query_text(query_text: str) -> str:
    """把价格类问法统一成“价格”，提升跨笔记向量召回的稳定性。"""
    for variant in _PRICE_SYNONYMS:
        query_text = query_text.replace(variant, "价格")
    return query_text


def _bigrams(text: str) -> list[str]:
    """中文按字切二元组（纯标点/空白分段丢弃），用于候选内的关键词重排。"""
    out: list[str] = []
    for span in re.findall(r"[一-鿿0-9a-zA-Z]+", text):
        if len(span) < 2:
            continue
        for i in range(len(span) - 1):
            out.append(span[i : i + 2])
    return out


def _query_bigrams(query_text: str) -> list[str]:
    """从问题中提取有区分度的二元组：去掉“是多少/是什么”等疑问尾巴；
    剩下按字切，单字段直接丢弃（无区分度）。"""
    cleaned = re.sub(r"(是多少|是什么|有哪些|怎么样|如何)$", "", query_text.strip())
    if len(cleaned) < 2:
        return []
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


def _lexical_rerank(
    query_text: str, candidates: list[dict], top_k: int
) -> list[dict]:
    """候选内 IDF 加权二元组重排：关键词命中的候选优先，
    词面完全不沾边的候选直接丢弃。

    （2026-10-03 实测：同样 30 候选，“监视器的价格是多少”经同义词归一后，
    “5. 专业剧组与租赁商价值”（含 8999 元定价）lex 排第一，而香水笔记
    的邻苯价格片段因二元组不命中被压到后面；纯 embedding 距离则把两者
    混在一起 0.5391 vs 0.5393 无法区分。）
    截断规则：只在最佳命中足够强（>= _LEX_MIN_BEST）时启用，
    保留 lex >= 最佳 * _LEX_RATIO 的候选；泛问（lex 普遍低）保持
    原 embedding 顺序，避免误杀。
    其余问题（同义词外）保持原 embedding 顺序。
    """
    query_text = _normalize_query_text(query_text)
    q_bigrams = set(_query_bigrams(query_text))
    if not q_bigrams or not candidates:
        return candidates[:top_k]
    n = len(candidates)
    doc_sets = [set(_bigrams(_normalize_query_text(c.get("text", "")))) for c in candidates]
    df: dict[str, int] = {}
    for s in doc_sets:
        for b in s:
            df[b] = df.get(b, 0) + 1
    idf = {b: math.log(1 + n / c) for b, c in df.items()}
    scored = []
    for cand, s in zip(candidates, doc_sets):
        lex = sum(idf.get(b, 0.0) for b in q_bigrams if b in s)
        scored.append((lex, cand))
    best_lex = max(s for s, _ in scored)
    if best_lex < _LEX_MIN_BEST:
        return candidates[:top_k]
    scored.sort(
        key=lambda item: (
            -item[0],
            item[1].get("distance")
            if item[1].get("distance") is not None
            else float("inf"),
        )
    )
    cutoff = best_lex * _LEX_RATIO
    filtered = [c for lex, c in scored if lex >= cutoff]
    return (filtered or [scored[0][1]])[:top_k]


GLOBAL_COLLECTION_NAME = "all_notes"

# 跨查全局召回规模：一次查多少候选，再按距离截断取 Top。
# 注意不能沿用单篇的“按篇配额召回”（每篇硬塞 meta1/md2/tr3），
# 否则无关笔记的弱相关片段也会被塞进上下文，来源数量虚胖、问答被带偏。
CROSS_CANDIDATES = 30
CROSS_TOP_K = 6
# 与最佳候选的距离差距超过该值视为弱相关，直接丢弃（相对截断，
# 比绝对阈值更稳：embedding 距离尺度随问题漂移，绝对值卡不准。
# 实测两篇真实笔记：价格问题 margin 0.1 时来源纯净（只剩本篇），
# 香水问题保持 6 条；0.15 则会漏进 2 条无关 meta/intro。）
CROSS_MARGIN = 0.1

# 单次跨查最多覆盖的笔记数（防 token 爆炸：配额按篇累加，上限兜底）
MAX_CROSS_NOTES = 50

# 词面重排的启用门限与截断比例（2026-10-03 按两篇验证笔记调参）：
# 最佳命中 < 2.0 视为泛问（各候选词面都弱），不启用重排，保持 embedding 顺序；
# 截断只保留 lex >= 最佳 * 0.5 的候选，词面不沾边的直接丢弃。
_LEX_MIN_BEST = 2.0
_LEX_RATIO = 0.5


def _note_title(note_data: dict, task_id: str) -> str:
    """从笔记 JSON 提取标题：audio_meta.title → markdown 首个 H 标题 → task_id 短写。"""
    audio_meta = note_data.get("audio_meta", {}) or {}
    title = (audio_meta.get("title") or "").strip()
    if title:
        return title
    markdown = note_data.get("markdown", "")
    if isinstance(markdown, list):
        markdown = markdown[-1].get("content", "") if markdown else ""
    if isinstance(markdown, str):
        for line in markdown.splitlines():
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                if title:
                    return title
    return task_id[:8]


class VectorStoreManager:
    """基于 ChromaDB 的笔记向量存储管理器。

    索引结构（跨笔记问答）：
    - 每篇笔记一个独立 collection（名称 = task_id）：历史行为，保持兼容，
      单篇问答继续走这里，旧索引不丢。
    - 全局 collection ``all_notes``：每条记录 metadata 带上 ``task_id`` /
      ``note_title``，跨笔记问答走这里。``index_task`` 双写两处。
    """

    def __init__(self):
        os.makedirs(VECTOR_DB_DIR, exist_ok=True)
        # 同一进程复用 client：多实例并发读写同一 sqlite 会间歇性
        # “HNSW segment reader: Nothing found on disk”。
        abs_dir = os.path.abspath(VECTOR_DB_DIR)
        client = _SHARED_CLIENTS.get(abs_dir)
        if client is None:
            client = chromadb.PersistentClient(
                path=VECTOR_DB_DIR,
                settings=Settings(anonymized_telemetry=False),
            )
            _SHARED_CLIENTS[abs_dir] = client
        self._client = client

    def _collection_name(self, task_id: str) -> str:
        """ChromaDB collection 名称：直接使用 task_id（UUID 格式合法）。"""
        return task_id

    def _get_global_collection(self):
        """获取全局跨笔记 collection（不存在则创建）。"""
        return self._client.get_or_create_collection(
            name=GLOBAL_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def index_task(self, task_id: str) -> None:
        """读取笔记结果并建立向量索引。

        双写：单篇 collection（历史行为，保持兼容）+ 全局 ``all_notes``
        collection（metadata 带 task_id / note_title，供跨笔记检索）。
        """
        result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")
        if not os.path.exists(result_path):
            logger.warning(f"笔记文件不存在，跳过索引: {result_path}")
            return

        with open(result_path, "r", encoding="utf-8") as f:
            note_data = json.load(f)

        markdown = note_data.get("markdown", "")
        if isinstance(markdown, list):
            # 多版本 markdown：取最新版参与索引
            markdown = markdown[-1].get("content", "") if markdown else ""
        transcript = note_data.get("transcript", {}) or {}
        segments = transcript.get("segments", []) or []

        audio_meta = note_data.get("audio_meta", {}) or {}

        meta_chunks = _build_meta_chunk(audio_meta)
        md_chunks = _chunk_markdown(markdown)
        tr_chunks = _chunk_transcript(segments)
        all_chunks = meta_chunks + md_chunks + tr_chunks

        if not all_chunks:
            logger.warning(f"笔记内容为空，跳过索引: {task_id}")
            return

        col_name = self._collection_name(task_id)

        # 删除旧 collection（幂等）
        try:
            self._client.delete_collection(col_name)
        except Exception:
            pass

        collection = self._client.create_collection(
            name=col_name,
            metadata={"hnsw:space": "cosine"},
        )

        documents = [c["text"] for c in all_chunks]
        metadatas = [c["metadata"] for c in all_chunks]
        ids = [f"{task_id}_{i}" for i in range(len(all_chunks))]

        collection.add(documents=documents, metadatas=metadatas, ids=ids)
        logger.info(f"向量索引完成: task_id={task_id}, chunks={len(all_chunks)}")

        # 双写全局 collection（失败不影响单篇索引）
        try:
            self._index_global(task_id, note_data, documents, metadatas, ids)
        except Exception as e:
            logger.warning(f"全局索引写入失败 (task_id={task_id}): {e}")

    def _index_global(
        self,
        task_id: str,
        note_data: dict,
        documents: list,
        metadatas: list,
        ids: list,
    ) -> None:
        """把同一批 chunks 写入全局 collection（先清该篇旧记录，保证幂等）。"""
        collection = self._get_global_collection()
        try:
            collection.delete(where={"task_id": task_id})
        except Exception:
            pass
        title = _note_title(note_data, task_id)
        global_metadatas = [
            {**m, "task_id": task_id, "note_title": title} for m in metadatas
        ]
        collection.add(
            documents=documents, metadatas=global_metadatas, ids=ids
        )
        logger.info(f"全局索引写入完成: task_id={task_id}, chunks={len(documents)}")

    def _parse_results(self, results: dict) -> list[dict]:
        """将 ChromaDB query 结果转换为 chunk 列表。"""
        chunks = []
        if not results or not results.get("documents") or not results["documents"][0]:
            return chunks
        for i in range(len(results["documents"][0])):
            chunks.append({
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "distance": results["distances"][0][i] if results["distances"] else None,
            })
        return chunks

    def query(
        self,
        task_id: str,
        query_text: str,
        n_results: int = 6,
        task_ids: Optional[list] = None,
    ) -> list[dict]:
        """
        按固定配额从各来源检索：meta 1 条、markdown 2 条、transcript 3 条，
        确保三种来源都被召回。

        ``task_ids`` 为 None 时走单篇 collection（历史行为）；
        传入非空 ``task_ids`` 时走全局 ``all_notes`` collection 并限定这些笔记。
        """
        if task_ids is not None:
            return self.query_cross(query_text, task_ids=task_ids)

        col_name = self._collection_name(task_id)
        try:
            collection = self._client.get_collection(col_name)
        except Exception:
            logger.warning(f"Collection 不存在: {col_name}")
            return []

        all_chunks = []

        # 每种来源的配额
        quotas = {"meta": 1, "markdown": 2, "transcript": 3}

        for source_type, quota in quotas.items():
            try:
                results = collection.query(
                    query_texts=[query_text],
                    n_results=quota,
                    where={"source_type": source_type},
                )
                all_chunks.extend(self._parse_results(results))
            except Exception:
                pass

        return all_chunks

    def query_cross(
        self, query_text: str, task_ids: Optional[list] = None
    ) -> list[dict]:
        """跨笔记检索：全局按语义距离统一召回 Top-K，不过滤出自哪篇笔记。

        ``task_ids`` 为空/None 时查全部已索引笔记；否则只查给定笔记。
        候选按 distance 升序截断 + 弱相关丢弃：无关笔记的片段不会被
        硬塞进上下文，来源数量即实际被引用的片段数。
        全局集合读失败（多写者并发的 HNSW 瞬态错）时回退到各单篇集合
        的配额召回，保证问答仍有来源而不是直接 0 条。
        """
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
        except Exception:
            logger.warning("全局 Collection 不存在，请先建立索引")
            return []

        where = None
        if task_ids is not None:
            task_ids = [t for t in task_ids if t][:MAX_CROSS_NOTES]
            if not task_ids:
                return []
            where = {"task_id": {"$in": task_ids}}

        try:
            results = collection.query(
                query_texts=[_normalize_query_text(query_text)],
                n_results=CROSS_CANDIDATES,
                where=where,
            )
        except Exception as e:
            logger.warning(f"跨笔记检索失败，回退单篇集合: {e}")
            return self._query_cross_fallback(query_text, task_ids)

        chunks = self._parse_results(results)
        if not chunks:
            return []
        # 按距离升序：与最佳候选差距超过 CROSS_MARGIN 的视为弱相关丢弃，
        # 再取 Top-K。无关笔记的片段不会被硬塞进上下文。
        ranked = sorted(
            chunks,
            key=lambda c: c["distance"] if c.get("distance") is not None else 0,
        )
        best = ranked[0].get("distance")
        if best is None:
            return _lexical_rerank(query_text, ranked, CROSS_TOP_K)
        kept = [
            c
            for c in ranked
            if c.get("distance") is None or c["distance"] - best <= CROSS_MARGIN
        ]
        return _lexical_rerank(query_text, kept, CROSS_TOP_K)

    def _local_note_task_ids(self) -> list:
        """扫描笔记目录兜底拿 task_id（全局集合不可读、indexed 为空时用）。"""
        try:
            names = sorted(
                f for f in os.listdir(NOTE_OUTPUT_DIR) if f.endswith(".json")
            )
        except OSError:
            return []
        out = []
        for name in names:
            if name.endswith(".status.json"):
                continue
            if "_transcript.json" in name or "_audio.json" in name:
                continue
            task_id = name[: -len(".json")]
            if "_" in task_id:
                continue
            out.append(task_id)
        return out

    def _fallback_note_title(self, task_id: str, cache: dict) -> str:
        """降级路径的标题查询：读笔记文件取标题，失败回 task_id 短写。"""
        if task_id in cache:
            return cache[task_id]
        title = task_id[:8]
        try:
            with open(
                os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"),
                "r",
                encoding="utf-8",
            ) as f:
                title = _note_title(json.load(f), task_id)
        except Exception:
            pass
        cache[task_id] = title
        return title

    def _query_cross_fallback(
        self, query_text: str, task_ids: Optional[list]
    ) -> list[dict]:
        """全局集合不可读时的降级：逐篇查单篇集合，按原配额召回。

        单篇集合的 metadata 没有 task_id/note_title（历史行为），这里补上，
        否则来源 badge 丢跨篇标记、点击跳转无目标——这就是“引用直接没了”
        的第二层成因。全局集合本身不可读时 indexed_task_ids 同样不可信，
        再退一层直接扫描笔记目录拿 task_id。
        """
        if task_ids is None:
            task_ids = []
            try:
                task_ids = self.indexed_task_ids()
            except Exception:
                task_ids = []
            if not task_ids:
                task_ids = self._local_note_task_ids()
        title_cache: dict[str, str] = {}
        per_note: list[list[dict]] = []
        for tid in (task_ids or [])[:MAX_CROSS_NOTES]:
            chunks = self.query(tid, _normalize_query_text(query_text), n_results=6)
            if chunks:
                for c in chunks:
                    meta = c.setdefault("metadata", {})
                    meta.setdefault("task_id", tid)
                    if not meta.get("note_title"):
                        meta["note_title"] = self._fallback_note_title(
                            tid, title_cache
                        )
                per_note.append(chunks)
        # 按篇轮取，保证多篇都有代表且总数不超过 Top-K
        merged: list[dict] = []
        for i in range(CROSS_TOP_K):
            for chunks in per_note:
                if i < len(chunks):
                    merged.append(chunks[i])
                if len(merged) >= CROSS_TOP_K:
                    return merged
        return merged

    def indexed_task_ids(self, limit: int = MAX_CROSS_NOTES) -> list:
        """返回全局索引中已建索引的 task_id 列表（按写入顺序去重）。"""
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
        except Exception:
            return []
        try:
            rows = collection.get(limit=min(max(limit * 6, 6), 10000))
        except Exception:
            return []
        seen = []
        for meta in rows.get("metadatas") or []:
            tid = (meta or {}).get("task_id")
            if tid and tid not in seen:
                seen.append(tid)
            if len(seen) >= limit:
                break
        return seen

    def is_indexed_global(self, task_id: str) -> bool:
        """检查该笔记在全局索引中是否存在。"""
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
            rows = collection.get(where={"task_id": task_id}, limit=1)
            return len(rows.get("ids") or []) > 0
        except Exception:
            return False

    def backfill_global(self, only_task_ids: Optional[list] = None) -> dict:
        """为缺失全局索引的历史笔记补建索引。

        扫描 ``note_results/*.json``：全局缺失但文件存在的逐个 ``index_task``
        （双写，单篇旧索引不受影响）。返回 ``{indexed, skipped, failed}``。
        """
        try:
            names = sorted(
                f for f in os.listdir(NOTE_OUTPUT_DIR) if f.endswith(".json")
            )
        except OSError:
            return {"indexed": [], "skipped": [], "failed": []}
        wanted = set(only_task_ids) if only_task_ids else None
        indexed, skipped, failed = [], [], []
        for name in names:
            if name.endswith(".status.json"):
                continue
            if "_transcript.json" in name or "_audio.json" in name:
                continue
            task_id = name[: -len(".json")]
            if "_" in task_id:
                continue
            if wanted is not None and task_id not in wanted:
                continue
            try:
                if self.is_indexed_global(task_id):
                    skipped.append(task_id)
                    continue
                self.index_task(task_id)
                indexed.append(task_id)
            except Exception as e:
                logger.warning(f"补索引失败 (task_id={task_id}): {e}")
                failed.append(task_id)
        return {"indexed": indexed, "skipped": skipped, "failed": failed}

    def delete_index(self, task_id: str) -> None:
        """删除指定任务的向量索引（单篇 collection + 全局记录）。"""
        col_name = self._collection_name(task_id)
        try:
            self._client.delete_collection(col_name)
            logger.info(f"已删除向量索引: {task_id}")
        except Exception:
            pass
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
            collection.delete(where={"task_id": task_id})
        except Exception:
            pass

    def is_indexed(self, task_id: str) -> bool:
        """检查指定任务是否已建立完整索引（含 meta 信息）。"""
        col_name = self._collection_name(task_id)
        try:
            col = self._client.get_collection(col_name)
            if col.count() == 0:
                return False
            # 检查是否包含 meta chunk，旧索引可能缺失
            meta = col.get(where={"source_type": "meta"}, limit=1)
            return len(meta["ids"]) > 0
        except Exception:
            return False

import json
import math
import os
import re
from typing import Optional

import chromadb
from chromadb.config import Settings

# 打包必需的显式导入：chromadb 1.x 把 API 实现与 telemetry 实现写成配置
# 字符串（chroma_api_impl / chroma_product_telemetry_impl），运行时动态加载，
# PyInstaller 静态分析发现不了。2026-10-03 起打包版 exe 里 PersistentClient
# 初始化全炸（No module named 'chromadb.telemetry.product.posthog' /
# 'chromadb.api.rust'），所有笔记向量索引失败、AI 问答双端全灭。
# 这两个都是纯 Python 模块（无额外二进制依赖），显式 import 后打包自动收录。
import chromadb.api.rust  # noqa: F401
import chromadb.telemetry.product.posthog  # noqa: F401

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
    # 兜底名「开头」：正文首个 H2 之前的引言块、或标题清洗后为空的章节。
    # 前端把 intro/开头 统一定位到笔记顶部（这类块没有可匹配的 heading，
    # 旧的 "intro" 兜底会让点击必报「未找到对应章节」）。
    return title or "开头"


def _chunk_markdown(markdown: str) -> list[dict]:
    """按 H2/H3 标题拆分 markdown 为语义块。"""
    sections = re.split(r'(?=^#{2,3}\s)', markdown, flags=re.MULTILINE)
    chunks = []
    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue
        heading_match = re.match(r'^(#{2,3})\s+(.+)', section)
        raw_title = heading_match.group(2).strip() if heading_match else "开头"
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


def _select_cross_top(
    query_text: str, candidates: list[dict], top_k: int
) -> list[dict]:
    """跨查候选统一筛选（全局路径与回退路径共用，保证来源纯净度一致）。

    1) 词面重排优先：问题有明确关键词（最佳 lex >= _LEX_MIN_BEST）时，
       只保留词面命中达最佳一半的候选——**无关笔记的片段在这一步被剔除**，
       哪怕它的 embedding 距离离问题更近。
    2) 词面通过后再过一道宽松距离闸（_CROSS_LOOSE_MARGIN，相对词面命中
       候选里的最佳距离），防止纯词面碰巧命中把语义上离题的片段捞回来。
    3) 泛问回退（词面普遍弱）：退回 embedding 距离 + CROSS_MARGIN 截断，
       保持旧行为。

    （2026-10-03 复盘用户实拍 bug：“猛玛极影7 ultra…价格…”问句 30 候选里，
    旧逻辑先用 CROSS_MARGIN=0.1 的距离截断再词面重排，导致词面最强、
    含 8999 元定价的“5. 专业剧组与租赁商价值”（距离 0.4234，超出 margin）
    被误杀，只剩 intro 弱相关块；而 HNSW 故障回退路径则每篇硬塞配额，
    香水笔记噪音直接混进引用来源。词面优先后两问题同解。）
    """
    if not candidates:
        return []
    query_text = _normalize_query_text(query_text)
    q_bigrams = set(_query_bigrams(query_text))
    if q_bigrams:
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
        if best_lex >= _LEX_MIN_BEST:
            cutoff = best_lex * _LEX_RATIO
            # 距离闸以“词面命中候选”的最佳距离为基准，而不是全体候选的
            # 最佳距离：无关笔记可能偶然离问题更近，用它会把真命中卡掉。
            # （2026-10-07 修复：旧实现注释写的是词面基准、代码却遍历全体
            # 候选取 min，注释与行为相反，真命中被无关节奏误杀。）
            base = min(
                (
                    c["distance"]
                    for lex, c in scored
                    if lex >= cutoff and c.get("distance") is not None
                ),
                default=None,
            )
            lex_kept = [
                (lex, cand)
                for lex, cand in scored
                if lex >= cutoff
                and (
                    cand.get("distance") is None
                    or base is None
                    or cand["distance"] - base <= _CROSS_LOOSE_MARGIN
                )
            ]
            if lex_kept:
                kept = [c for _, c in lex_kept]
                kept.sort(
                    key=lambda c: c.get("distance")
                    if c.get("distance") is not None
                    else float("inf")
                )
                return kept[:top_k]
            # 词面命中全部被距离闸拦下：保底给词面最强的单条，宁可少给
            # 也不给噪音。
            best_pair = max(scored, key=lambda item: item[0])
            return [best_pair[1]]
    ranked = sorted(
        candidates,
        key=lambda c: c["distance"] if c.get("distance") is not None else 0,
    )
    best = ranked[0].get("distance")
    if best is None:
        return candidates[:top_k]
    kept = [
        c
        for c in ranked
        if c.get("distance") is None or c["distance"] - best <= CROSS_MARGIN
    ]
    return kept[:top_k]


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
# 词面过滤通过后附加的宽松距离闸：相对“词面命中候选”的最佳距离。
# 比主距离闸（CROSS_MARGIN=0.1）松——词面已经证明强相关，这里只拦
# 语义上明显跑题的纯词面巧合（如问句里常见的“价格”二字）。
_CROSS_LOOSE_MARGIN = 0.25


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
            # 必须抛错而不是静默跳过：_do_index 会把静默返回标成 indexed
            # （假成功），前端拿着假状态进问答却检索不到任何内容。
            raise ValueError(f"笔记源文件不存在，无法索引: {result_path}")

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
            raise ValueError(f"笔记内容为空，无法索引: {task_id}")

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
            except Exception as e:
                # 单来源查询失败不该拖垮整次检索，但不能无声吞掉——
                # 之前 transcript 查询坏了只会静默少一类来源，排查无门。
                logger.warning(f"单篇检索 {source_type} 来源失败: {e}")

        return all_chunks

    def query_cross(
        self,
        query_text: str,
        task_ids: Optional[list] = None,
        top_k: int = CROSS_TOP_K,
    ) -> list[dict]:
        """跨笔记检索：全局按语义距离统一召回 Top-K，不过滤出自哪篇笔记。

        ``task_ids`` 为空/None 时查全部已索引笔记；否则只查给定笔记。
        ``top_k`` 可调（chat/ask 用默认值；MCP /chat/search 按需传更大的值）。
        候选先过词面重排（_select_cross_top）：无关笔记的片段不会被
        硬塞进上下文，来源数量即实际被引用的片段数。
        全局集合不存在（老安装从未建过 all_notes）或读失败（多写者并发的
        HNSW 瞬态错）时，先尝试从单篇集合原地重建全局集合并重查一次；
        仍失败则回退到各单篇集合召回 + 同一套筛选，保证问答仍有来源且
        来源纯净度不打折。（旧实现在"集合不存在"分支直接静默返空，
        全局索引缺位的用户每次跨查都拿到零来源。）
        """
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
        except Exception:
            logger.warning("全局 Collection 不存在，尝试从单篇集合重建")
            collection = None

        where = None
        if task_ids is not None:
            task_ids = [t for t in task_ids if t][:MAX_CROSS_NOTES]
            if not task_ids:
                return []
            where = {"task_id": {"$in": task_ids}}

        chunks = (
            self._try_query_global(collection, query_text, where)
            if collection is not None
            else None
        )
        if chunks is None:
            # 全局集合缺失或坏了（典型：多进程写入后 HNSW 段失效，查询报
            # "Error creating hnsw index"）：先重建再试一次，重建失败
            # 或仍查不动才降级到单篇集合。
            logger.warning("全局集合不可用，尝试重建后重查")
            if self._rebuild_global_from_per_note():
                try:
                    collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
                    chunks = self._try_query_global(collection, query_text, where)
                except Exception as e:
                    logger.warning(f"重建后重查仍失败: {e}")
                    chunks = None
            if chunks is None:
                return self._query_cross_fallback(query_text, task_ids, top_k)

        if not chunks:
            return []
        return _select_cross_top(query_text, chunks, top_k)

    def _try_query_global(
        self, collection, query_text: str, where: Optional[dict]
    ) -> Optional[list[dict]]:
        """执行全局集合查询，任何异常都吞掉返回 None（需重建/回退）。"""
        try:
            return self._query_global(collection, query_text, where)
        except Exception as e:
            logger.warning(f"全局集合查询失败: {e}")
            return None

    def _query_global(
        self, collection, query_text: str, where: Optional[dict]
    ) -> Optional[list[dict]]:
        """执行全局集合查询。返回 None 表示查询失败（需重建/回退）。"""
        try:
            results = collection.query(
                query_texts=[_normalize_query_text(query_text)],
                n_results=CROSS_CANDIDATES,
                where=where,
            )
            return self._parse_results(results)
        except Exception as e:
            logger.warning(f"全局集合查询失败: {e}")
            return None

    def _rebuild_global_from_per_note(self) -> bool:
        """从各单篇集合重建全局 ``all_notes``（多进程写入后 HNSW 段
        失效的自愈路径：段文件坏掉时查询必挂，而单篇集合是好的——
        直接把单篇内容搬回全局集合，比重新嵌入全文便宜且不依赖笔记文件）。
        """
        try:
            docs: list[str] = []
            metas: list[dict] = []
            ids: list[str] = []
            title_cache: dict[str, str] = {}
            for col in self._client.list_collections():
                name = col.name if hasattr(col, "name") else str(col)
                if name == GLOBAL_COLLECTION_NAME:
                    continue
                try:
                    got = self._client.get_collection(name).get(limit=10000)
                except Exception:
                    continue
                documents = got.get("documents") or []
                if not documents:
                    continue
                title = self._fallback_note_title(name, title_cache)
                for doc, meta, cid in zip(
                    documents, got.get("metadatas") or [], got.get("ids") or []
                ):
                    docs.append(doc)
                    metas.append(
                        {**(meta or {}), "task_id": name, "note_title": title}
                    )
                    ids.append(f"{name}:{cid}")
            if not docs:
                return False
            try:
                self._client.delete_collection(GLOBAL_COLLECTION_NAME)
            except Exception:
                pass
            collection = self._client.create_collection(
                name=GLOBAL_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            # 分批写，避免大库一次 add 超时
            batch = 200
            for i in range(0, len(docs), batch):
                collection.add(
                    documents=docs[i : i + batch],
                    metadatas=metas[i : i + batch],
                    ids=ids[i : i + batch],
                )
            logger.info(f"全局集合重建完成: chunks={len(docs)}")
            return True
        except Exception as e:
            logger.warning(f"全局集合重建失败: {e}")
            return False

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
            # 历史 checkpoint 残留（如 {uuid}.gpt.checkpoint.json，旧格式无
            # _markdown 中缀）：不是笔记正文，混进来会让 coverage 虚高、
            # backfill 对着一个不存在正文的文件反复失败。
            if ".gpt.checkpoint" in name:
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
        self, query_text: str, task_ids: Optional[list], top_k: int = CROSS_TOP_K
    ) -> list[dict]:
        """全局集合不可读时的降级：逐篇查单篇集合。

        旧版按篇配额召回后轮询合并——每篇硬塞 meta1/md2/tr3，无关笔记的
        弱相关片段必然混进引用来源（2026-10-03 用户实拍：问猛玛价格，
        引用一半来自香水笔记）。现在合并全部候选后走与全局路径相同的
        ``_select_cross_top`` 筛选，噪音在词面/距离两道闸被剔掉。

        单篇集合的 metadata 没有 task_id/note_title（历史行为），这里补上，
        否则来源 badge 丢跨篇标记、点击跳转无目标。全局集合本身不可读时
        indexed_task_ids 同样不可信，再退一层直接扫描笔记目录拿 task_id。
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
        merged: list[dict] = []
        for tid in (task_ids or [])[:MAX_CROSS_NOTES]:
            chunks = self.query(tid, _normalize_query_text(query_text), n_results=6)
            if not chunks:
                continue
            for c in chunks:
                meta = c.setdefault("metadata", {})
                meta.setdefault("task_id", tid)
                if not meta.get("note_title"):
                    meta["note_title"] = self._fallback_note_title(
                        tid, title_cache
                    )
            merged.extend(chunks)
        return _select_cross_top(query_text, merged, top_k)

    def indexed_task_ids(self, limit: int = MAX_CROSS_NOTES) -> list:
        """返回全局索引中已建索引的 task_id 列表（按写入顺序去重）。

        注意不能"取 limit*6 行再去重截断"：每篇笔记约 10~50 个 chunk，
        取 200*6=1200 行时只覆盖前几十篇，96 篇的库只能看到 29 个
        （2026-10-04 实测：sqlite 里 96 个 distinct task，API 只返回 29）。
        这里分页拉全量去重后再按 limit 截断；调用方传大 limit 即拿全量。
        """
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
        except Exception:
            return []
        seen: list[str] = []
        seen_set: set[str] = set()
        offset = 0
        page = 2000
        try:
            while True:
                rows = collection.get(
                    limit=page,
                    offset=offset,
                    include=["metadatas"],
                )
                metas = rows.get("metadatas") or []
                if not metas:
                    break
                for meta in metas:
                    tid = (meta or {}).get("task_id")
                    if tid and tid not in seen_set:
                        seen_set.add(tid)
                        seen.append(tid)
                    if len(seen) >= limit:
                        return seen[:limit]
                if len(metas) < page:
                    break
                offset += page
        except Exception:
            pass
        return seen[:limit]

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
            if ".gpt.checkpoint" in name:
                # 同 _local_note_task_ids：checkpoint 残留不是笔记正文
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

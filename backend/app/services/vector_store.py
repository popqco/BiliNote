import json
import os
import re
from typing import Optional

import chromadb
from chromadb.config import Settings

from app.utils.logger import get_logger

logger = get_logger(__name__)

NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", "note_results")
VECTOR_DB_DIR = os.getenv("VECTOR_DB_DIR", "vector_db")


def _chunk_markdown(markdown: str) -> list[dict]:
    """按 H2/H3 标题拆分 markdown 为语义块。"""
    sections = re.split(r'(?=^#{2,3}\s)', markdown, flags=re.MULTILINE)
    chunks = []
    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue
        heading_match = re.match(r'^(#{2,3})\s+(.+)', section)
        title = heading_match.group(2).strip() if heading_match else "intro"
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


GLOBAL_COLLECTION_NAME = "all_notes"

# 跨笔记检索时每篇笔记的来源配额（与单篇检索保持一致：meta 1、markdown 2、transcript 3）
_SOURCE_QUOTAS = {"meta": 1, "markdown": 2, "transcript": 3}

# 单次跨查最多覆盖的笔记数（防 token 爆炸：配额按篇累加，上限兜底）
MAX_CROSS_NOTES = 50


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
        self._client = chromadb.PersistentClient(
            path=VECTOR_DB_DIR,
            settings=Settings(anonymized_telemetry=False),
        )

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
        """跨笔记检索：查全局 collection，每篇笔记按来源配额召回。

        ``task_ids`` 为空/None 时查全部已索引笔记；否则只查给定笔记。
        每篇笔记内部复用“meta 1 / markdown 2 / transcript 3”配额。
        """
        try:
            collection = self._client.get_collection(GLOBAL_COLLECTION_NAME)
        except Exception:
            logger.warning("全局 Collection 不存在，请先建立索引")
            return []

        if task_ids is not None:
            task_ids = [t for t in task_ids if t][:MAX_CROSS_NOTES]
            if not task_ids:
                return []
        else:
            task_ids = self.indexed_task_ids(limit=MAX_CROSS_NOTES)
            if not task_ids:
                return []

        all_chunks = []
        for tid in task_ids:
            for source_type, quota in _SOURCE_QUOTAS.items():
                try:
                    results = collection.query(
                        query_texts=[query_text],
                        n_results=quota,
                        where={
                            "$and": [
                                {"source_type": source_type},
                                {"task_id": tid},
                            ]
                        },
                    )
                    all_chunks.extend(self._parse_results(results))
                except Exception:
                    continue
        return all_chunks

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

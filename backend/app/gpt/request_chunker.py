import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class ChunkPayload:
    segments: list
    image_urls: list
    # 分批元信息（chunk_time_aligned 填写）：批次序号/总数/时间范围，
    # summarize 据此在每批 prompt 里注入「第 i/N 段」说明；旧路径保持 None。
    meta: Optional[dict] = None


class RequestChunker:
    def __init__(self, message_builder: Callable, max_bytes: int, size_estimator: Optional[Callable] = None,
                 max_images_per_chunk: Optional[int] = None):
        """
        :param max_images_per_chunk: 单个请求最多带几张图。只按字节数分块是不够的：
            图片的**token**开销与字节数不成正比，一张 1568px 拼图约 800~1500 tokens，
            长视频攒出上百张时，即使字节数没超限，上游也会因上下文过大而拒/断
            （2026-10-01 实测 2h53m 视频 64 张拼图，免费网关连续 APIConnectionError）。
            超出的图会顺延到新的分块，覆盖率不变，只是拆成多次请求。
        """
        self.message_builder = message_builder
        self.max_bytes = max_bytes
        self.size_estimator = size_estimator
        self.max_images_per_chunk = max_images_per_chunk

    def _images_full(self, chunk: ChunkPayload) -> bool:
        return (
            self.max_images_per_chunk is not None
            and len(chunk.image_urls) >= self.max_images_per_chunk
        )

    def estimate(self, messages) -> int:
        if self.size_estimator:
            return self.size_estimator(messages)
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _messages_size(self, segments, image_urls, **kwargs) -> int:
        messages = self.message_builder(segments, image_urls, **kwargs)
        return self.estimate(messages)

    def _get_text(self, segment) -> str:
        if isinstance(segment, dict):
            return segment.get("text", "")
        return getattr(segment, "text", "")

    def _make_segment(self, segment, text: str):
        if isinstance(segment, dict):
            new_seg = dict(segment)
            new_seg["text"] = text
            return new_seg
        if hasattr(segment, "__dict__"):
            data = dict(segment.__dict__)
            data["text"] = text
            return type(segment)(**data)
        return type(segment)(segment.start, segment.end, text)

    def _split_segment_to_fit(self, segment, **kwargs):
        text = self._get_text(segment)
        if not text:
            raise ValueError("empty segment cannot be split")
        lo, hi = 1, len(text)
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = self._make_segment(segment, text[:mid])
            size = self._messages_size([candidate], [], **kwargs)
            if size <= self.max_bytes:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            raise ValueError("single segment too large to fit request")
        head = self._make_segment(segment, text[:best])
        tail = self._make_segment(segment, text[best:])
        return head, tail

    def chunk(self, segments: list, image_urls: list, **kwargs) -> List[ChunkPayload]:
        segments = list(segments or [])
        image_urls = list(image_urls or [])
        if not segments and not image_urls:
            return []

        chunks: List[ChunkPayload] = []
        seg_idx = 0

        while seg_idx < len(segments):
            batch_segments = []
            while seg_idx < len(segments):
                candidate = batch_segments + [segments[seg_idx]]
                size = self._messages_size(candidate, [], **kwargs)
                if size <= self.max_bytes:
                    batch_segments = candidate
                    seg_idx += 1
                    continue
                if not batch_segments:
                    head, tail = self._split_segment_to_fit(segments[seg_idx], **kwargs)
                    segments[seg_idx] = head
                    segments.insert(seg_idx + 1, tail)
                    continue
                break

            if not batch_segments:
                raise ValueError("unable to fit any content into chunk")

            chunks.append(ChunkPayload(segments=batch_segments, image_urls=[]))

        if not image_urls:
            return chunks

        if not chunks:
            chunks = [ChunkPayload(segments=[], image_urls=[])]

        if not segments:
            for image in image_urls:
                appended = False
                for chunk in chunks[-1:]:
                    if self._images_full(chunk):
                        continue
                    candidate_images = chunk.image_urls + [image]
                    if self._messages_size(chunk.segments, candidate_images, **kwargs) <= self.max_bytes:
                        chunk.image_urls = candidate_images
                        appended = True
                        break

                if appended:
                    continue

                if self._messages_size([], [image], **kwargs) > self.max_bytes:
                    raise ValueError("single image payload exceeds max_bytes")
                chunks.append(ChunkPayload(segments=[], image_urls=[image]))
            return chunks

        chunk_count = len(chunks)
        total_images = len(image_urls)
        for idx, image in enumerate(image_urls):
            preferred_idx = min(chunk_count - 1, (idx * chunk_count) // total_images)
            placed = False

            for chunk_idx in range(preferred_idx, len(chunks)):
                chunk = chunks[chunk_idx]
                if self._images_full(chunk):
                    continue
                candidate_images = chunk.image_urls + [image]
                if self._messages_size(chunk.segments, candidate_images, **kwargs) <= self.max_bytes:
                    chunk.image_urls = candidate_images
                    placed = True
                    break

            if placed:
                continue

            if self._messages_size([], [image], **kwargs) > self.max_bytes:
                raise ValueError("single image payload exceeds max_bytes")
            chunks.append(ChunkPayload(segments=[], image_urls=[image]))

        return chunks

    def chunk_time_aligned(self, segments: list, image_urls: list, **kwargs) -> Optional[List[ChunkPayload]]:
        """时间对齐分批（2026-10-11）：转写按时间轴切 N 个连续窗口，拼图按时间
        占比摊进各窗，让每一批同时含「这段的转写 + 这段的截图」。

        背景：chunk() 按字节数切文本，45MB 预算下转写几乎永远整个装进第 1 块，
        超出的拼图全部溢出成 segments=[] 的纯图块——模型看图时没有任何上下文
        （2026-10-11 摸底确认）。本方法把切分依据从「字节」换成「批次数」：
        N = ceil(拼图数 / max_images_per_chunk)，文本窗口与拼图窗口一一对应。

        :returns: 批次列表；无法满足约束（转写段数不足以开出足够窗口、或某批
            超 max_bytes）时返回 None，调用方回退 chunk() 旧逻辑。
        """
        segments = list(segments or [])
        image_urls = list(image_urls or [])
        if not image_urls:
            return None
        per_chunk = max(1, int(self.max_images_per_chunk or 20))
        if not segments:
            return None  # 纯图无文本，交回旧逻辑处理

        total_batches = min(math.ceil(len(image_urls) / per_chunk), len(segments))
        if total_batches < 2:
            return None  # 一批就装得下，不需要分

        def text_bytes(seg) -> int:
            t = self._get_text(seg)
            return len(t.encode("utf-8")) if t else 1  # 空段至少算 1，防除零

        # 按文本字节数近似均分出 total_batches 个时间连续、非空的窗口
        total_text = sum(text_bytes(s) for s in segments)
        windows: List[list] = []
        target = total_text / total_batches
        acc = 0
        current: list = []
        for i, seg in enumerate(segments):
            current.append(seg)
            acc += text_bytes(seg)
            segs_left = len(segments) - i - 1
            windows_left = total_batches - len(windows) - 1  # 本窗之后还要开的窗数（含尾窗）
            if acc >= target and windows_left >= 1 and segs_left >= windows_left:
                windows.append(current)
                remaining_text = total_text - sum(text_bytes(s) for w in windows for s in w)
                target = remaining_text / (total_batches - len(windows))
                acc = 0
                current = []
        if current:
            windows.append(current)
        if len(windows) < total_batches:
            return None

        # 拼图按时间顺序连续等分到各窗（floor/ceil 分布，每窗 ≤ per_chunk）
        chunk_count = len(windows)
        if math.ceil(len(image_urls) / chunk_count) > per_chunk:
            return None

        def seg_time(seg, key):
            if isinstance(seg, dict):
                return seg.get(key)
            return getattr(seg, key, None)

        chunks: List[ChunkPayload] = []
        for j, win in enumerate(windows):
            lo = (j * len(image_urls)) // chunk_count
            hi = ((j + 1) * len(image_urls)) // chunk_count
            time_start = seg_time(win[0], "start") if win else None
            time_end = seg_time(win[-1], "end") if win else None
            chunks.append(ChunkPayload(
                segments=win,
                image_urls=image_urls[lo:hi],
                meta={
                    "batch_index": j + 1,
                    "batch_total": chunk_count,
                    "time_start": time_start,
                    "time_end": time_end,
                },
            ))

        # 兜底字节校验：正常构造下单批 ≈ 文本窗口 + 20 张拼图 ≈ 6MB ≪ 45MB，
        # 但单个巨型 segment 或异常大的 base64 都可能超限——宁可回退旧逻辑
        for c in chunks:
            try:
                size = self._messages_size(c.segments, c.image_urls, **kwargs)
            except Exception:
                return None
            if size > self.max_bytes:
                return None
        return chunks

    def group_texts_by_budget(self, texts: List[str], build_messages: Callable, **kwargs) -> List[List[str]]:
        groups: List[List[str]] = []
        idx = 0
        while idx < len(texts):
            group: List[str] = []
            while idx < len(texts):
                candidate = group + [texts[idx]]
                try:
                    messages = build_messages(candidate, [], **kwargs)
                except TypeError:
                    messages = build_messages(candidate, **kwargs)
                size = self.estimate(messages)
                if size <= self.max_bytes:
                    group = candidate
                    idx += 1
                    continue
                if not group:
                    raise ValueError("single text block exceeds max_bytes")
                break
            groups.append(group)
        return groups

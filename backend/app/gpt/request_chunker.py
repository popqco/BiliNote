from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class ChunkPayload:
    segments: list
    image_urls: list


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

from app.gpt.base import GPT
from app.gpt.prompt_builder import generate_base_prompt
from app.models.gpt_model import GPTSource
import logging
import os
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.gpt.prompt import BASE_PROMPT, AI_SUM, SCREENSHOT, LINK, MERGE_PROMPT
from app.gpt.utils import fix_markdown
from app.gpt.request_chunker import RequestChunker
from app.models.transcriber_model import TranscriptSegment
from datetime import timedelta
from typing import List

from app.utils.logger import get_logger

# 统一 get_logger，保证记录进 logs/app.log（裸 logging.getLogger 无 handler 会丢日志）
logger = get_logger(__name__)


class EmptyCompletionError(RuntimeError):
    """上游完成请求但返回了空内容（推理预算被吃光 / 边缘节点截断等）。

    单独定型以便限次重试后快速失败，交给上层的降级阶梯换更小的请求
    （见 docs/adr/0003），而不是当成网络错误重试到天荒地老。
    """


class CompletionStalled(RuntimeError):
    """流式响应长时间不再吐出内容（上游假死 / 中间层慢慢滴心跳）。

    httpx 的 read 超时对**流式**请求是按「两次收到字节之间」计时的，上游只要
    定期发一个空分片就能一直吊着连接——实测有任务因此卡在「总结中」十几分钟，
    两个 worker 全被占死，用户看到的现象就是「一直排队 / 一直不动」。
    这里加两道闸：静默超时（多久没收到正文）和绝对上限（单次调用总时长）。
    """


class UniversalGPT(GPT):
    def __init__(self, client, model: str, temperature: float = 0.7):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.screenshot = False
        self.link = False
        self.max_request_bytes = int(os.getenv("OPENAI_MAX_REQUEST_BYTES", str(45 * 1024 * 1024)))
        # 单请求图片数上限。实测（2026-10-01，opencode.ai/zen + space-bunny-free）：
        # 21 张 1568px 拼图 / 6.7MB → 正常返回；64 张 2880px 拼图 / 60MB → 连续
        # APIConnectionError / 空内容。图片 token 开销与字节数不成正比，只卡字节
        # 不够，再卡一道张数（见 RequestChunker.max_images_per_chunk）。
        self.max_images_per_request = int(os.getenv("OPENAI_MAX_IMAGES_PER_REQUEST", "20") or 20)
        self.checkpoint_dir = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # 初始化时缓存重试配置，避免每次请求重复读取环境变量
        self._max_retry_attempts = max(1, int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3")))
        self._retry_base_backoff = float(os.getenv("OPENAI_RETRY_BACKOFF_SECONDS", "1.5"))

    def _format_time(self, seconds: float) -> str:
        return str(timedelta(seconds=int(seconds)))[2:]

    def _build_segment_text(self, segments: List[TranscriptSegment]) -> str:
        return "\n".join(
            f"{self._format_time(seg.start)} - {seg.text.strip()}"
            for seg in segments
        )

    def ensure_segments_type(self, segments) -> List[TranscriptSegment]:
        return [TranscriptSegment(**seg) if isinstance(seg, dict) else seg for seg in segments]

    def create_messages(self, segments: List[TranscriptSegment], **kwargs):

        content_text = generate_base_prompt(
            title=kwargs.get('title'),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get('tags'),
            _format=kwargs.get('_format'),
            style=kwargs.get('style'),
            extras=kwargs.get('extras'),
        )

        video_img_urls = kwargs.get('video_img_urls', [])

        content: list[dict] | str
        if video_img_urls:
            # 有截图时走 OpenAI 多模态 content 数组（text + image_url）
            content = [{"type": "text", "text": content_text}]
            for url in video_img_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": url,
                        "detail": "auto"
                    }
                })
        else:
            # 纯文本场景退回 string content：DeepSeek deepseek-chat 等非多模态模型
            # 不识别 [{"type":"text",...}] 数组形态，会返回 invalid_request_error
            # （issue #282）。OpenAI 规范本身也允许 content 为 string。
            content = content_text

        messages = [{
            "role": "user",
            "content": content
        }]

        return messages

    def list_models(self):
        return self.client.models.list()

    def _estimate_messages_bytes(self, messages: list) -> int:
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = MERGE_PROMPT + "\n\n" + "\n\n---\n\n".join(partials)
        # 合并阶段没有图片，直接用 string content 兼容非多模态模型（issue #282）
        return [{
            "role": "user",
            "content": merge_text
        }]

    def _checkpoint_path(self, checkpoint_key: str) -> Path:
        safe_key = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in checkpoint_key)
        return self.checkpoint_dir / f"{safe_key}.gpt.checkpoint.json"

    def _build_source_signature(self, source: GPTSource) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_request_bytes": self.max_request_bytes,
            "max_images_per_request": self.max_images_per_request,
            "title": source.title,
            "tags": source.tags,
            "format": source._format,
            "style": source.style,
            "extras": source.extras,
            "video_img_urls": source.video_img_urls or [],
            "segments": [
                {
                    "start": getattr(seg, "start", None),
                    "end": getattr(seg, "end", None),
                    "text": getattr(seg, "text", "")
                }
                for seg in source.segment
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_checkpoint(self, checkpoint_key: str, source_signature: str) -> dict | None:
        path = self._checkpoint_path(checkpoint_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source_signature") != source_signature:
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _save_checkpoint(self, checkpoint_key: str, source_signature: str, partials: list, phase: str) -> None:
        path = self._checkpoint_path(checkpoint_key)
        data = {
            "version": 1,
            "source_signature": source_signature,
            "phase": phase,
            "partials": partials,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _clear_checkpoint(self, checkpoint_key: str) -> None:
        self._checkpoint_path(checkpoint_key).unlink(missing_ok=True)

    @staticmethod
    def _is_insufficient_quota_error(exc: Exception) -> bool:
        raw = str(exc)
        return (
            "insufficient_user_quota" in raw
            or "预扣费额度失败" in raw
            or "insufficient quota" in raw.lower()
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        raw = str(exc).lower()
        retryable_tokens = (
            "error code: 524",
            "bad_response_status_code",
            "timed out",
            "timeout",
            "rate limit",
            "error code: 429",
            "error code: 500",
            "error code: 502",
            "error code: 503",
            "error code: 504",
            "apiconnectionerror",
            "connection error",
            "service unavailable",
            # openai SDK 对不可解析的响应体抛的通用文案（多为边缘节点/代理抽风），值得重试
            "unknown error",
            # 免费网关间歇性空回：openai._streaming.json 对空响应体抛
            # JSONDecodeError（2026-10-05 连败两条笔记，稍后同网关成功，
            # 证明一次重试本就能救回来）
            "expecting value",
            "jsondecodeerror",
        )
        if any(token in raw for token in retryable_tokens):
            return True

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status in {408, 409, 429, 500, 502, 503, 504, 524}

    @staticmethod
    def _is_temperature_unsupported_error(exc: Exception) -> bool:
        """OpenAI o1/o3/gpt-5 系列等新模型不接受自定义 temperature，
        只允许默认值 1，传 0.7 会报 `'temperature' does not support 0.7 ...`。"""
        raw = str(exc).lower()
        return "temperature" in raw and (
            "does not support" in raw
            or "unsupported_value" in raw
            or "only the default" in raw
        )

    def _do_create(self, messages: list):
        """单次调用（流式）。

        为什么用 stream=True：zen 免费推理模型（space-bunny-free 等）对长提示
        先输出大段 reasoning_content 再给正式回答，非流式请求在整段生成完毕前
        一个字节都不返回——超过 Cloudflare ~100s 的源站超时后连接被直接掐断
        （RemoteDisconnected），且生成时间随提示长度膨胀到数分钟。流式下 SSE
        响应头立刻到达、token 持续流动，不会被边缘超时误杀。
        如果模型拒绝自定义 temperature，就地去掉该参数再试一次（不消耗外层
        重试次数预算），仍失败则把异常抛给外层重试逻辑。"""
        try:
            return self._stream_create(messages, temperature=self.temperature)
        except Exception as exc:
            if self._is_temperature_unsupported_error(exc):
                print(f"[universal_gpt] 模型 {self.model} 不支持自定义 temperature，改用默认值重试")
                return self._stream_create(messages, temperature=None)
            raise

    def _stream_create(self, messages: list, temperature):
        from types import SimpleNamespace
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        parts: list[str] = []
        finish_reason = None
        # 两道闸，单位秒，可用环境变量覆盖：
        #   OPENAI_STREAM_STALL_SECONDS    多久没收到任何增量就判定上游假死（默认 120）
        #   OPENAI_STREAM_DEADLINE_SECONDS 单次调用总时长上限（默认 900）
        stall_limit = float(os.getenv("OPENAI_STREAM_STALL_SECONDS", "120") or 120)
        total_limit = float(os.getenv("OPENAI_STREAM_DEADLINE_SECONDS", "900") or 900)
        started_at = time.monotonic()
        last_activity_at = started_at
        for chunk in stream:
            now = time.monotonic()
            if now - last_activity_at > stall_limit:
                raise CompletionStalled(
                    f"上游 {stall_limit:.0f}s 没有任何增量（静默超时，model={self.model}）"
                )
            if now - started_at > total_limit:
                raise CompletionStalled(
                    f"单次调用超过 {total_limit:.0f}s 上限（model={self.model}）"
                )
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = choice.delta
            text = getattr(delta, "content", None)
            if text:
                parts.append(text)
            # 任何一段增量都算「上游还活着」，包括推理模型先吐的 reasoning_content。
            # 只认 content 会把「正在思考」误判成假死：space-bunny-free 这类模型在长
            # 提示上先流几分钟推理才给正文，2h53m 视频的 full 策略就是这样被 120s
            # 闸门掐掉的（2026-10-01 实测，日志里只留下一句 CompletionStalled）。
            if text or getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None):
                last_activity_at = now
        content = "".join(parts)
        if not content.strip():
            raise EmptyCompletionError(
                f"上游返回空内容 (model={self.model}, finish_reason={finish_reason})"
            )
        # 包装成与非流式响应同形的对象，调用方只读 choices[0].message.content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )

    def _chat_completion_create(self, messages: list):
        last_exc = None
        for attempt in range(self._max_retry_attempts):
            try:
                return self._do_create(messages)
            except EmptyCompletionError as exc:
                # 空响应只小额度重试（最多 2 次）：偶发抖动值得再试一次，
                # 但更可能是请求太大把上游打崩——快速失败让降级阶梯换更小的请求
                last_exc = exc
                limit = min(2, self._max_retry_attempts)
                if attempt >= limit - 1:
                    raise
                logger.warning(f"[universal_gpt] {exc}，重试 ({attempt + 1}/{limit})")
                time.sleep(self._retry_base_backoff * (2 ** attempt))
            except Exception as exc:
                last_exc = exc
                # 假死超时不在这里重试：同一个大请求再发一次大概率还是挂，
                # 直接抛给上层的降级阶梯换成更小的请求（ADR-0003）。
                if isinstance(exc, CompletionStalled):
                    raise
                if attempt == self._max_retry_attempts - 1 or not self._is_retryable_error(exc):
                    raise
                sleep_seconds = self._retry_base_backoff * (2 ** attempt)
                logger.warning(
                    f"[universal_gpt] 调用失败（{type(exc).__name__}: {str(exc)[:200]}），"
                    f"{sleep_seconds:.1f}s 后重试 ({attempt + 1}/{self._max_retry_attempts})"
                )
                time.sleep(sleep_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    def _merge_partials(self, partials: list, checkpoint_key: str | None, source_signature: str | None) -> str:
        def build_messages(texts, *_args, **_kwargs):
            return self._build_merge_messages(texts)

        merge_chunker = RequestChunker(
            lambda *_args, **_kwargs: [],
            self.max_request_bytes,
            self._estimate_messages_bytes
        )

        current_partials = list(partials)
        while len(current_partials) > 1:
            groups = merge_chunker.group_texts_by_budget(current_partials, build_messages)
            new_partials = []
            for group_idx, group in enumerate(groups):
                messages = build_messages(group)
                try:
                    response = self._chat_completion_create(messages)
                except Exception as exc:
                    if checkpoint_key and source_signature:
                        self._save_checkpoint(checkpoint_key, source_signature, current_partials, "merge")
                    raise

                new_partials.append(response.choices[0].message.content.strip())

                if checkpoint_key and source_signature:
                    remaining_partials = []
                    for remaining_group in groups[group_idx + 1:]:
                        remaining_partials.extend(remaining_group)
                    resumable_partials = new_partials + remaining_partials
                    self._save_checkpoint(checkpoint_key, source_signature, resumable_partials, "merge")

            current_partials = new_partials

        return current_partials[0]

    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        checkpoint_key = source.checkpoint_key
        source_signature = self._build_source_signature(source) if checkpoint_key else None

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(
            message_builder,
            self.max_request_bytes,
            self._estimate_messages_bytes,
            max_images_per_chunk=self.max_images_per_request,
        )

        try:
            chunks = chunker.chunk(
                source.segment,
                source.video_img_urls or [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
        except ValueError:
            chunks = chunker.chunk(
                source.segment,
                [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )

        partials = []
        if checkpoint_key and source_signature:
            checkpoint = self._load_checkpoint(checkpoint_key, source_signature)
            if checkpoint and isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]

        if len(partials) > len(chunks):
            partials = []

        for chunk in chunks[len(partials):]:
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
            try:
                response = self._chat_completion_create(messages)
            except Exception as exc:
                if checkpoint_key and source_signature:
                    self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")
                raise

            partials.append(response.choices[0].message.content.strip())
            if checkpoint_key and source_signature:
                self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")

        if len(partials) == 1:
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        return merged

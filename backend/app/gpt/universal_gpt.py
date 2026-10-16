from app.gpt.base import GPT
from app.gpt.prompt_builder import generate_base_prompt
from app.models.gpt_model import GPTSource
import logging
import os
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from app.gpt.prompt import BASE_PROMPT, AI_SUM, SCREENSHOT, LINK, MERGE_PROMPT
from app.gpt.utils import fix_markdown
from app.gpt.request_chunker import RequestChunker
from app.models.transcriber_model import TranscriptSegment
from datetime import timedelta
from typing import List, Optional

from app.utils.logger import get_logger

# 统一 get_logger，保证记录进 logs/app.log（裸 logging.getLogger 无 handler 会丢日志）
logger = get_logger(__name__)


def _feed_strategy() -> str:
    """喂图策略：segmented（默认，时间对齐分批）| single（旧字节分块）。

    函数化而非常量：source_signature 里要引用它，且测试里可以用
    monkeypatch.setenv 在运行时切换。
    """
    return (os.getenv("SUMMARY_FEED_STRATEGY", "segmented") or "segmented").strip().lower()


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
    def __init__(self, client, model: str, temperature: float = 0.7, thinking_effort: Optional[str] = None):
        self.client = client
        self.model = model
        self.temperature = temperature
        # 供应商级思考强度（reasoning effort，2026-10-11）：None = 不传该参数，
        # 行为与从前完全一致；非法档位由上游报错 + 去参重试兜底。
        self.thinking_effort = (thinking_effort or "").strip().lower() or None
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
        # 分批喂图（chunk_time_aligned）时给每批注入「第 i/N 段」说明，
        # 让模型知道这只是整条视频的一部分，输出本段笔记待合并
        chunk_note = str(kwargs.get("chunk_note") or "")
        if chunk_note:
            content_text = chunk_note + "\n\n" + content_text

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
        prompt = MERGE_PROMPT
        # 分批喂图时每批笔记都自带目录/总结段，多层合并的去重会整段丢掉它们
        # （2026-10-14 实测：55 批合并后最终笔记或缺「AI 总结」或缺「目录」）。
        # 这些段落本身就是分批阶段的临时产物（每批一份「本段指出…」），
        # 现在统一在合并后由 finalize_note 确定性重建，合并阶段全部丢弃。
        fmt = getattr(self, "_format", None) or []
        if "toc" in fmt or "summary" in fmt:
            prompt += (
                "- 各片段中自带的「## 目录」与「## AI 总结」段落请**全部丢弃**"
                "（包括片段里出现的多条目录/总结），不要在合并结果里保留："
                "它们会由系统在合并完成后统一重新生成\n"
                "- 正文里也不要出现「本段」「本批」这类措辞\n"
            )
        merge_text = prompt + "\n\n" + "\n\n---\n\n".join(partials)
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
            # 喂图策略影响分批组成；换策略后旧 checkpoint 的 partials 必然对不上
            "feed_strategy": _feed_strategy(),
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

    @staticmethod
    def _is_reasoning_unsupported_error(exc: Exception) -> bool:
        """网关/模型不认识 reasoning_effort（非推理模型、旧网关、规范外档位
        xhigh/max 被拒等）：报错会同时出现参数名与「未知参数」类字样。只匹配
        这类显式拒绝，避免把「推理预算超限」等无关报错误判成参数错误。"""
        raw = str(exc).lower()
        has_param = "reasoning" in raw or "effort" in raw
        rejection = (
            "unknown parameter" in raw
            or "unrecognized" in raw
            or "unsupported" in raw
            or "does not support" in raw
            or "not supported" in raw
            or "invalid" in raw
        )
        return has_param and rejection

    def _do_create(self, messages: list):
        """单次调用（流式）。

        为什么用 stream=True：zen 免费推理模型（space-bunny-free 等）对长提示
        先输出大段 reasoning_content 再给正式回答，非流式请求在整段生成完毕前
        一个字节都不返回——超过 Cloudflare ~100s 的源站超时后连接被直接掐断
        （RemoteDisconnected），且生成时间随提示长度膨胀到数分钟。流式下 SSE
        响应头立刻到达、token 持续流动，不会被边缘超时误杀。
        如果模型拒绝自定义 temperature 或 reasoning_effort，就地去掉对应参数
        再试一次（不消耗外层重试次数预算），仍失败则把异常抛给外层重试逻辑。"""
        effort = self.thinking_effort
        try:
            return self._stream_create(messages, temperature=self.temperature, reasoning_effort=effort)
        except Exception as exc:
            if self._is_temperature_unsupported_error(exc):
                print(f"[universal_gpt] 模型 {self.model} 不支持自定义 temperature，改用默认值重试")
                return self._stream_create(messages, temperature=None, reasoning_effort=effort)
            if effort and self._is_reasoning_unsupported_error(exc):
                print(f"[universal_gpt] 模型 {self.model} 不接受 reasoning_effort={effort}，去参重试")
                return self._stream_create(messages, temperature=self.temperature, reasoning_effort=None)
            raise

    def _resolve_stall_limit(self) -> float:
        """流式内容静默阈值（秒）：环境变量 > 客户端按链路的建议值 > 120。

        Responses 链路要宽得多：网关（cmdc-pak / OpenCode Free 实测）把推理内容
        藏起来，模型思考期只在线上发 `: ping` 保活注释（SDK 会丢弃注释），
        正文 delta 之前内容层可以静默几分钟——用 120s 判定「上游挂死」纯属误杀，
        会白白触发减帧降级、牺牲笔记质量（2026-10-17：19/20 次假死是 muse-spark，
        全部发生在 Responses 链路）。chat 链路推理是流式可见的，静默即异常，
        保持 120s 快速失败。
        """
        env = os.getenv("OPENAI_STREAM_STALL_SECONDS")
        if env:
            try:
                value = float(env)
                if value > 0:
                    return value
            except ValueError:
                pass
        hinted = getattr(getattr(self, "client", None), "stream_stall_seconds", None)
        if isinstance(hinted, (int, float)) and hinted > 0:
            return float(hinted)
        return 120.0

    def _stream_create(self, messages: list, temperature, reasoning_effort: Optional[str] = None):
        from types import SimpleNamespace
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        stream = self.client.chat.completions.create(**kwargs)
        parts: list[str] = []
        finish_reason = None
        # 两道闸，单位秒，可用环境变量覆盖：
        #   OPENAI_STREAM_STALL_SECONDS    多久没收到任何增量就判定上游假死
        #                                  （默认 120；Responses 链路按 600，见 _resolve_stall_limit）
        #   OPENAI_STREAM_DEADLINE_SECONDS 单次调用总时长上限（默认 900）
        stall_limit: Optional[float] = None
        total_limit = float(os.getenv("OPENAI_STREAM_DEADLINE_SECONDS", "900") or 900)
        started_at = time.monotonic()
        last_activity_at = started_at
        for chunk in stream:
            now = time.monotonic()
            if stall_limit is None:
                # 首个增量到达时才解析：此时协议路由已定（可能刚发生过换协议），
                # 按实际链路取静默预算——思考期不可见的 responses 链路要宽得多。
                stall_limit = self._resolve_stall_limit()
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
            # keepalive（协议适配层透传的结构性事件，2026-10-17）同理算活跃。
            if (
                text
                or getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or getattr(delta, "keepalive", False)
            ):
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

    @staticmethod
    def _format_batch_ts(seconds) -> str:
        try:
            s = int(float(seconds))
        except (TypeError, ValueError):
            return "?"
        return f"{s // 60}:{s % 60:02d}"

    def _batch_note(self, meta: Optional[dict]) -> str:
        """把分批元信息转成注入每批 prompt 的说明文字（旧路径 meta 为 None）。"""
        if not meta:
            return ""
        total = int(meta.get("batch_total") or 0)
        if total <= 1:
            return ""
        return (
            f"【分批说明】整条视频的转写与截图被分成 {total} 批依次发给你，"
            f"本批是第 {meta.get('batch_index')}/{total} 批"
            f"（对应原片 {self._format_batch_ts(meta.get('time_start'))} - "
            f"{self._format_batch_ts(meta.get('time_end'))}）。"
            "请只基于本批的转写与截图撰写这一段的笔记（仍按完整格式输出），"
            "系统随后会把各批笔记合并成整篇。"
            "本批只写正文：**不要输出「## 目录」与「## AI 总结」**"
            "（这两个整篇级段落由系统在合并后统一生成），也不要用「本段」「本批」"
            "这类措辞描述整体。"
        )

    def _write_summary(self, body: str) -> str:
        """基于整篇笔记正文重新生成一份 AI 总结（合并后整理的第二步）。

        分批阶段每批各写一份的「本段…」小结拼贴成不了总结；这里合并完成后
        用一次纯文本调用统一重写：总览 + 关键要点，供读者快速浏览取用。

        总结规模按正文信息量自适应（2026-10-15 用户反馈：固定「2~4 句 +
        3~6 条」对不同信息密度的视频太死板）：以二级标题数量为信息量代理
        （没有标题时退回正文字符数），短笔记 2~3 句 + 3~5 条、中等 3~4 句 +
        4~8 条、长笔记 4~6 句 + 6~12 条；区间内按密度取上下限、宁少勿滥。
        """
        sections = len(re.findall(r"(?m)^##\s+\S", body))
        if sections:
            tier = 0 if sections <= 8 else (1 if sections <= 30 else 2)
        else:
            tier = 0 if len(body) < 4000 else (1 if len(body) < 20000 else 2)
        overview_range, points_range = (
            ("2~3", "3~5") if tier == 0 else
            ("3~4", "4~8") if tier == 1 else
            ("4~6", "6~12")
        )
        prompt = (
            "以下是一条视频的完整笔记（Markdown）。请你**只基于笔记内容**写一份"
            "「AI 总结」，供读者快速浏览时取用：\n"
            f"- 先用 {overview_range} 句话概括：这条视频讲了什么、作者的最终观点或结论是什么；\n"
            f"- 然后列出 {points_range} 条最值得记住的关键信息（每条独立一行，以「- 」开头，"
            "一条一句话，点到信息本体——具体事件、数字、结论，而不是“讲了什么话题”）；\n"
            "- 句数与条数在区间内按信息密度取舍：内容少的取区间下限，信息密集的可以逼近上限；"
            "宁少勿滥，只保留读者扫一眼就能带走的结论、数字与关键事实；\n"
            "- 直接输出总结正文的 Markdown：不要输出标题「AI 总结」，不要用代码块包裹；\n"
            "- 严禁出现「本段」「本批」「本条视频」「整篇笔记」这类措辞；"
            "不要罗列章节流程或写作过程，只写结论与要点。\n\n"
            "笔记正文如下：\n\n" + body
        )
        response = self._chat_completion_create([{"role": "user", "content": prompt}])
        text = (response.choices[0].message.content or "").strip()
        # 兜底：模型偶尔仍自带标题或代码块围栏
        text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        text = re.sub(r"^#{1,4}\s*AI\s*总结[：:]?\s*\n+", "", text)
        return text.strip()

    def finalize_note(self, markdown: str) -> str:
        """合并完成后的确定性整理（2026-10-14 用户实拍「本段指出」堆叠总结的修复）。

        1. 摘掉正文里所有「## 目录」「## AI 总结」段落（含各批残留的多份）；
        2. 勾了目录 → 从二级标题确定性重建一份目录（带时间入口，不调模型）；
        3. 勾了总结 → 统一重写一份 AI 总结（纯文本调用；失败则回退保留旧总结）。
        幂等：整理过的笔记再整理一次结果不变（目录/总结是重建出来的）。
        """
        from app.utils.note_helper import build_toc_block, strip_global_sections

        fmt = list(getattr(self, "_format", None) or [])
        body, old_summary = strip_global_sections(markdown)
        if not body.strip():
            logger.warning("[universal_gpt] finalize：正文为空，按原样返回")
            return markdown

        blocks: list[str] = []
        if "toc" in fmt:
            toc = build_toc_block(body, link_enabled=bool(self.link))
            if toc:
                blocks.append(toc)
            else:
                logger.info("[universal_gpt] finalize：正文没有可用的二级标题，跳过目录重建")
        blocks.append(body.strip())
        if "summary" in fmt:
            summary = ""
            try:
                summary = self._write_summary(body)
            except Exception as e:
                logger.warning(f"[universal_gpt] AI 总结重写失败，回退保留原总结：{e}")
                summary = old_summary
            if summary.strip():
                blocks.append("## AI 总结\n\n" + summary.strip())
        return "\n\n".join(blocks).strip() + "\n"

    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        # 记下用户勾选的格式（toc/link/screenshot/summary）——合并阶段要用它决定
        # 是否在最终笔记里保留/补上「AI 总结」段落
        self._format = list(source._format or [])
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

        # 喂图策略（2026-10-11）：默认 segmented——拼图数超过单请求上限时，
        # 按「时间对齐」分批（转写窗口与拼图窗口一一对应，消灭无文本纯图块）；
        # 设 SUMMARY_FEED_STRATEGY=single 回到旧行为（45MB 字节切分，图溢出
        # 成纯图块），适合强行单次全量喂给大上下文模型。
        chunks = None
        if _feed_strategy() == "segmented" and source.video_img_urls:
            try:
                chunks = chunker.chunk_time_aligned(
                    source.segment,
                    source.video_img_urls,
                    title=source.title,
                    tags=source.tags,
                    _format=source._format,
                    style=source.style,
                    extras=source.extras,
                )
            except Exception as e:
                logger.warning(f"时间对齐分批失败，回退按字节分块：{e}")
                chunks = None
        try:
            if chunks is None:
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

        batch_total = sum(1 for c in chunks if getattr(c, "meta", None))
        if batch_total > 1:
            logger.info(f"时间对齐分批喂图：{batch_total} 批（共 {len(source.video_img_urls or [])} 张拼图）")

        partials = []
        if checkpoint_key and source_signature:
            checkpoint = self._load_checkpoint(checkpoint_key, source_signature)
            if checkpoint and isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]

        if len(partials) > len(chunks):
            partials = []

        # 分批执行（2026-10-11 起支持并发，task_config 的 summarize_batch_concurrency
        # 控制）：各批之间无依赖，只是最后 merge 需要汇总。结果严格按批次索引归位，
        # checkpoint 只落「连续前缀」——乱序并发完成时断点续跑语义与旧串行格式完全兼容。
        base = len(partials)
        remaining = chunks[base:]
        results: list = list(partials) + [None] * len(remaining)

        def _save_progress():
            if not (checkpoint_key and source_signature):
                return
            end = base
            while end < len(results) and results[end] is not None:
                end += 1
            if end > base:
                self._save_checkpoint(checkpoint_key, source_signature, results[:end], "summarize")

        def _run_chunk(chunk) -> str:
            # 分批阶段不产出「目录」「AI 总结」：这两个是整篇级段落，每批各写
            # 一份会在合并后堆成拼贴（2026-10-14 实拍：55 批「本段指出…」
            # 叠在最终笔记的 AI 总结里）。合并完成后由 finalize_note 统一重建。
            batch_format = [f for f in (source._format or []) if f not in ("toc", "summary")]
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=batch_format,
                style=source.style,
                extras=source.extras,
                chunk_note=self._batch_note(getattr(chunk, "meta", None)),
            )
            response = self._chat_completion_create(messages)
            return response.choices[0].message.content.strip()

        # 函数内导入：本文件会被测试用 stub 模块独立加载，顶部 import app.services
        # 会让单独运行时炸 ImportError（app stub 无 __path__）。
        from app.services.task_config_manager import TaskConfigManager

        concurrency = max(1, TaskConfigManager().get("summarize_batch_concurrency"))
        if concurrency > 1 and len(remaining) > 1:
            logger.info(f"分批总结并发执行：{len(remaining)} 批，并发 {concurrency}")
            with ThreadPoolExecutor(max_workers=min(concurrency, len(remaining))) as pool:
                futures = {pool.submit(_run_chunk, c): base + i for i, c in enumerate(remaining)}
                try:
                    for fut in as_completed(futures):
                        results[futures[fut]] = fut.result()
                        _save_progress()
                except Exception:
                    # 任一批失败：取消未开跑的余批，已完成进度落 checkpoint，
                    # 抛错交给上层降级阶梯（full→grid2→grid3→thinned→text_only）
                    for f in futures:
                        f.cancel()
                    _save_progress()
                    raise
        else:
            for i, chunk in enumerate(remaining):
                try:
                    results[base + i] = _run_chunk(chunk)
                except Exception:
                    _save_progress()
                    raise
                _save_progress()

        partials = [r for r in results if r is not None]

        if len(partials) == 1:
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        # 合并完成后的确定性整理：摘掉各批堆积的目录/总结、重建目录、重写 AI 总结
        # （最终返回的已是整理后的整篇笔记；整理失败按未整理内容返回，不拖垮任务）
        try:
            merged = self.finalize_note(merged)
        except Exception as e:
            logger.warning(f"[universal_gpt] 合并后整理失败，按原样返回：{e}")
        return merged

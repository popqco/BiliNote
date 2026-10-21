import json
import logging
import math
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple, Union, Any

from fastapi import HTTPException
from pydantic import HttpUrl
from dotenv import load_dotenv

from app.downloaders.base import Downloader
from app.downloaders.bilibili_downloader import BilibiliDownloader
from app.downloaders.douyin_downloader import DouyinDownloader
from app.downloaders.local_downloader import LocalDownloader
from app.downloaders.youtube_downloader import YoutubeDownloader
from app.db.video_task_dao import delete_task_by_video, insert_video_task
from app.db.model_dao import get_model_by_provider_and_name
from app.enmus.exception import NoteErrorEnum, ProviderErrorEnum
from app.enmus.task_status_enums import TERMINAL_STATUSES, TaskStatus
from app.enmus.note_enums import DownloadQuality
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.exceptions.task_cancel import TaskCancelled
from app.gpt.base import GPT
from app.gpt.gpt_factory import GPTFactory
from app.models.audio_model import AudioDownloadResult
from app.models.gpt_model import GPTSource
from app.models.model_config import ModelConfig
from app.models.notes_model import AudioDownloadResult, NoteResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.paths import NOTE_OUTPUT_DIR
from app.services.constant import SUPPORT_PLATFORM_MAP
from app.services.provider import ProviderService
from app.services.task_config_manager import TaskConfigManager
from app.services.task_serial_executor import (
    LOCAL_TRANSCRIBER_TYPES,
    local_transcribe_semaphore,
    online_transcribe_semaphore,
)
from app.services.task_runs import (
    TaskRun,
    finish_run,
    is_run_cancelled,
    register_run,
)
from app.services import task_state
from app.services.task_state.runs import (
    active_task_for_video,
    is_task_active,
    mark_task_active,
    mark_task_done,
)
from app.services.video_meta import fetch_video_meta
from app.transcriber.base import Transcriber
from app.transcriber.segmented import SAFETY_FACTOR, FLOOR_BYTES_PER_SEC, cleanup_part_caches, probe_duration, transcribe_segmented
from app.transcriber.transcriber_provider import get_transcriber, _transcribers
from app.utils.note_helper import (
    cleanup_marker_residue,
    normalize_math_delimiters,
    prepend_source_link,
    replace_content_markers,
)
from app.utils.note_quality import NoteContentError, ensure_note_content
from app.utils.logger import get_logger
from app.utils.path_helper import get_app_dir
from app.utils.screenshot_marker import extract_screenshot_timestamps, strip_screenshot_markers
from app.utils.status_code import StatusCode
from app.utils.video_helper import generate_screenshot
from app.utils.video_reader import VideoReader

# ------------------ 环境变量与全局配置 ------------------

# 从 .env 文件中加载环境变量
load_dotenv()

# 后端 API 地址与端口（若有需要可以在代码其他部分使用 BACKEND_BASE_URL）
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost")
BACKEND_PORT = os.getenv("BACKEND_PORT", "8483")
BACKEND_BASE_URL = f"{API_BASE_URL}:{BACKEND_PORT}"

# 输出目录（用于缓存音频、转写、Markdown 文件，以及存储截图）
IMAGE_OUTPUT_DIR = os.getenv("OUT_DIR", "./static/screenshots")
# 图片基础 URL（用于生成 Markdown 中的图片链接，需前端静态目录对应）
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "/static/screenshots")

# 日志配置：必须用统一 get_logger（会挂 logs/app.log 的文件 handler）。
# 裸 logging.getLogger 没有 handler，记录会全部丢失——2026-10-01 排查总结阶段
# 「日志消失」时确认的根因。
logger = get_logger(__name__)

# 视频理解帧预算（见 _adapt_frame_budget）：超过时自动逐级升格/拉间隔。
# 2026-10-11 起 UI 可调（设置→通用「生成性能与预算」，落盘 config/task_config.json），
# 取值优先级：配置文件 > 环境变量 > 默认 4800/1000（激进档；旧档 600/150、2400/500）。
def _frame_budget() -> int:
    return TaskConfigManager().get("frame_budget")


def _image_budget() -> int:
    return TaskConfigManager().get("image_budget")

# 非终态判定用（提交去重 / 任务列表）。CANCELLED 也是终态：用户停掉的任务
# 不该再拦住同视频的重新提交，也不该被 reap 当成「执行中」。
# 定义已移到 app.enmus.task_status_enums，这里保留导入名供历史调用点使用。


def _should_segment(audio_file: str, max_bytes: int) -> bool:
    """是否需要走分段转写：ffprobe 成功、原始文件超上限、且就算压到下限码率
    （16kbps，whisper 仍可用）也放不进上限。任一探测异常都按 False 兜底走
    原路径——既有 fit_size_audio 会给出明确的「仍超限」报错。"""
    try:
        if os.path.getsize(audio_file) <= max_bytes:
            return False
        duration = probe_duration(audio_file)
        if duration <= 0:
            return False
        return duration * FLOOR_BYTES_PER_SEC > max_bytes * SAFETY_FACTOR
    except Exception as e:
        logger.warning(f"分段转写触发判定探测失败，按整件处理：{e}")
        return False


import re as _re


class NoteGenerator:
    """
    NoteGenerator 用于执行视频/音频下载、转写、GPT 生成笔记、插入截图/链接、
    以及将任务信息写入状态文件与数据库等功能。
    """

    # 本次执行的 run（取消信号 + 状态写入归属）：generate() 里绑定。
    # 类级默认值：绕过 __init__ 构造的实例（测试替身/直接方法调用）也能
    # 安全地走 _cancel_requested()，不需要每个调用方都先赋值。
    _run: Optional[TaskRun] = None

    def __init__(self):
        from app.services.transcriber_config_manager import TranscriberConfigManager
        config_manager = TranscriberConfigManager()
        self.model_size: str = config_manager.get_whisper_model_size()
        self.device: Optional[str] = None
        self.transcriber_type: str = config_manager.get_transcriber_type()
        self.transcriber: Transcriber = self._init_transcriber()
        self.video_path: Optional[Path] = None
        self.video_img_urls = []
        self._last_frame_interval: Optional[int] = None
        self._last_grid_size: Optional[List[int]] = None
        # 取消语义：绑定前的写状态调用（如路由提交时的 PENDING）不受抑制影响
        self._run: Optional[TaskRun] = None
        logger.info("NoteGenerator 初始化完成")


    # ---------------- 取消检查（用户点「停止生成」） ----------------

    def _cancel_requested(self) -> bool:
        """是否已请求停止本次执行（供下游组件当谓词用）。"""
        return is_run_cancelled(self._run)

    def _check_cancelled(self) -> None:
        """阶段边界/重试间隙的取消检查点：命中即抛 TaskCancelled。"""
        if self._cancel_requested():
            raise TaskCancelled()

    def _acquire_cancellable(self, sem, timeout: float = 1.0) -> None:
        """带取消检查的信号量获取。

        排队等 GPU 转写信号量时也要能停：直接 `with sem:` 会在前一个任务
        跑完前一直阻塞，用户点了停止也只能干等（2026-10-16 需求场景）。
        """
        while True:
            self._check_cancelled()
            if sem.acquire(timeout=timeout):
                return


    # ---------------- 公有方法 ----------------

    def generate(
        self,
        video_url: Union[str, HttpUrl],
        platform: str,
        quality: DownloadQuality = DownloadQuality.medium,
        task_id: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        link: bool = False,
        screenshot: bool = False,
        _format: Optional[List[str]] = None,
        style: Optional[str] = None,
        extras: Optional[str] = None,
        output_path: Optional[str] = None,
        video_understanding: bool = False,
        video_interval: int = 0,
        grid_size: Optional[List[int]] = None,
        run: Optional[TaskRun] = None,
    ) -> NoteResult | None:
        """
        主流程：按步骤依次下载、转写、GPT 总结、截图/链接处理、存库、返回 NoteResult。

        :param run: 本次执行的取消登记（run_note_task 在进线程池前创建）；
                    直接调用（测试/脚本）时缺省自建。
        :param video_url: 视频或音频链接
        :param platform: 平台名称，对应 SUPPORT_PLATFORM_MAP 中的键
        :param quality: 下载音频的质量枚举
        :param task_id: 用于标识本次任务的唯一 ID，亦用于状态文件和缓存文件命名
        :param model_name: GPT 模型名称
        :param provider_id: 模型供应商 ID
        :param link: 是否在笔记中插入视频片段链接
        :param screenshot: 是否在笔记中替换 Screenshot 标记为图片
        :param _format: 包含 'link' 或 'screenshot' 等字符串的列表，决定后续处理
        :param style: GPT 生成笔记的风格
        :param extras: 额外参数，传递给 GPT
        :param output_path: 下载输出目录（可选）
        :param video_understanding: 是否需要视频拼图理解（生成缩略图）
        :param video_interval: 视频帧截取间隔（秒），仅在 video_understanding 为 True 时生效
        :param grid_size: 生成缩略图时的网格大小，如 [3, 3]
        :return: NoteResult 对象，包含 markdown 文本、转写结果和音频元信息
        """
        if grid_size is None:
            grid_size = []

        # 绑定本次执行的 run：register_run 会作废同 task_id 的旧 run（重试场景），
        # 并把「提交后立刻点了停止」的墓碑转成本 run 的取消信号
        if run is None and task_id:
            run = register_run(task_id)
        self._run = run
        downloader = None

        try:
            self._check_cancelled()
            logger.info(f"开始生成笔记 (task_id={task_id})")
            self._update_status(task_id, TaskStatus.PARSING)

            # 获取下载器与 GPT 实例

            downloader = self._get_downloader(platform)
            # 下载阶段（长视频音轨/视频体量不小）把取消检查挂进 yt-dlp 进度回调，
            # 点停止后正在跑的下载立即中断，不必等整条下完
            try:
                downloader.cancel_check = self._cancel_requested
            except Exception:
                pass
            gpt = self._get_gpt(model_name, provider_id)
            # 总结阶段（含分批喂图 + 降级阶梯 + 阶梯内网络重试）的取消检查：
            # UniversalGPT 在流式增量与重试间隙调用谓词
            try:
                gpt.cancel_check = self._cancel_requested
            except Exception:
                pass
            self._check_cancelled()

            # 缓存文件路径
            audio_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_audio.json"
            transcript_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_transcript.json"
            markdown_cache_file = NOTE_OUTPUT_DIR / f"{task_id}_markdown.md"
            # 1. 获取字幕/转写：优先缓存 → 平台字幕 → 音频转写
            transcript = None

            # 尝试读取缓存
            if transcript_cache_file.exists():
                logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
                try:
                    data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                    segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                    transcript = TranscriptResult(
                        language=data.get("language"),
                        full_text=data["full_text"],
                        segments=segments,
                    )
                    logger.info(f"已从缓存加载转写结果，共 {len(segments)} 段")
                except Exception as e:
                    logger.warning(f"加载转写缓存失败: {e}")

            # 缓存没有，尝试获取平台字幕
            if transcript is None:
                self._check_cancelled()
                logger.info("尝试获取平台字幕（优先于音频下载）...")
                try:
                    transcript = downloader.download_subtitles(video_url)
                    if transcript and transcript.segments:
                        logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                        transcript_cache_file.write_text(
                            json.dumps(asdict(transcript), ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    else:
                        transcript = None
                        logger.info("平台无可用字幕，将下载音频后转写")
                except TaskCancelled:
                    raise
                except Exception as e:
                    logger.warning(f"获取平台字幕失败: {e}，将下载音频后转写")
                    transcript = None

            # 2. 下载音轨/视频
            # 音轨只为「本地 whisper 转写」服务：已有平台字幕时 mp3 没有下游消费者，
            # 再下一整条音轨纯属浪费（2h53m 的视频 = 多传 ~170MB），而且凭空多一次
            # 失败机会——2026-10-01 就是这步撞上 CDN SSL 断流，把一条字幕/视频都齐了的
            # 任务判死。视频帧要不要下与字幕无关，由截图/视频理解开关决定。
            has_transcript = transcript is not None
            audio_meta = self._download_media(
                downloader=downloader,
                video_url=video_url,
                quality=quality,
                audio_cache_file=audio_cache_file,
                status_phase=TaskStatus.DOWNLOADING,
                platform=platform,
                output_path=output_path,
                screenshot=screenshot,
                video_understanding=video_understanding,
                video_interval=video_interval,
                grid_size=grid_size,
                skip_audio=has_transcript,
                task_id=task_id,
            )

            # 拿到元信息的第一时间就把标题/封面写进状态文件：前端据此提前显示卡片，
            # 不再等任务成功/失败后才知道是哪个视频（见 docs/adr/0002）
            self._merge_status_meta(task_id, {
                "audio_meta": {
                    "title": audio_meta.title,
                    "cover_url": audio_meta.cover_url,
                    "video_id": audio_meta.video_id,
                    "duration": audio_meta.duration,
                    "platform": platform,
                }
            })

            # 3. 如果前面没拿到字幕，走转写流程
            if transcript is None:
                transcript = self._get_transcript(
                    downloader=downloader,
                    video_url=video_url,
                    audio_file=audio_meta.file_path,
                    transcript_cache_file=transcript_cache_file,
                    status_phase=TaskStatus.TRANSCRIBING,
                    task_id=task_id,
                )

            # 3. GPT 总结（失败自动进入降级阶梯：原样 → 减帧 → 纯文本）
            self._check_cancelled()
            markdown = self._summarize_text(
                audio_meta=audio_meta,
                transcript=transcript,
                gpt=gpt,
                markdown_cache_file=markdown_cache_file,
                link=link,
                screenshot=screenshot,
                formats=_format or [],
                style=style,
                extras=extras,
                video_img_urls=self.video_img_urls,
                task_id=task_id,
                video_interval=video_interval,
                grid_size=grid_size,
            )

            # 4. 后处理：公式定界符归一化（无条件执行，不受格式开关影响）+
            # 截图 & 链接替换（按格式开关执行）
            self._check_cancelled()
            try:
                markdown = normalize_math_delimiters(markdown)
            except Exception as e:
                logger.warning(f"公式归一化失败，跳过该步骤：{e}")
            if _format:
                markdown = self._post_process_markdown(
                    markdown=markdown,
                    video_path=self.video_path,
                    formats=_format,
                    audio_meta=audio_meta,
                    platform=platform,
                )

            markdown = prepend_source_link(markdown, str(video_url))

            # 完成前的最后一道闸：_summarize_text 已校验过，这里再兜一次，
            # 确保任何后处理/未来改动都不会把空壳笔记写成 SUCCESS。
            ensure_note_content(
                markdown,
                getattr(transcript, "full_text", "") or "\n".join(
                    (getattr(seg, "text", "") or "")
                    for seg in getattr(transcript, "segments", [])
                ),
            )

            # 5. 保存记录到数据库
            self._update_status(task_id, TaskStatus.SAVING)
            self._save_metadata(video_id=audio_meta.video_id, platform=platform, task_id=task_id)

            # 6. 完成
            self._update_status(task_id, TaskStatus.SUCCESS)
            logger.info(f"笔记生成成功 (task_id={task_id})")
            return NoteResult(
                markdown=markdown,
                transcript=transcript,
                audio_meta=audio_meta,
                model_name=model_name,
                provider_id=provider_id,
                style=style,
            )

        except TaskCancelled:
            logger.info(f"任务已被用户停止 (task_id={task_id})")
            self._update_status(task_id, TaskStatus.CANCELLED, message="已停止生成（用户手动停止）")
            return None
        except Exception as exc:
            # 用户已点停止时按「已停止」收尾：下游（yt-dlp / 转写 / 上游 SDK）
            # 可能把取消异常包成自己的错误类型抛出来，不能因此落成红字 FAILED
            if self._cancel_requested():
                logger.info(f"任务在停止处理中退出，按已停止收尾 (task_id={task_id})：{exc}")
                self._update_status(task_id, TaskStatus.CANCELLED, message="已停止生成（用户手动停止）")
                return None
            logger.error(f"生成笔记流程异常 (task_id={task_id})：{exc}", exc_info=True)
            self._update_status(task_id, TaskStatus.FAILED, message=self._format_error(exc))
            return None
        finally:
            # 清理任务专属的帧/拼图临时目录（并发隔离方案的配套清理）
            self._cleanup_task_scratch(task_id)
            # 下载器是每任务独立实例（取消谓词不跨任务）；顺带释放它生成的临时
            # cookiefile 等资源。
            if downloader is not None:
                try:
                    downloader.cleanup()
                except Exception:
                    pass
            # 摘除本次 run 登记：仅当自己仍是当前 run 时才摘
            finish_run(self._run)

    @staticmethod
    def delete_note(video_id: str, platform: str) -> int:
        """
        删除数据库中对应 video_id 与 platform 的任务记录

        :param video_id: 视频 ID
        :param platform: 平台标识
        :return: 删除的记录数
        """
        logger.info(f"删除笔记记录 (video_id={video_id}, platform={platform})")
        return delete_task_by_video(video_id, platform)

    # ---------------- 私有方法 ----------------

    def _init_transcriber(self) -> Transcriber:
        """
        根据环境变量 TRANSCRIBER_TYPE 动态获取并实例化转写器
        """
        if self.transcriber_type not in _transcribers:
            logger.error(f"未找到支持的转写器：{self.transcriber_type}")
            raise Exception(f"不支持的转写器：{self.transcriber_type}")

        logger.info(f"使用转写器：{self.transcriber_type}")
        return get_transcriber(
            transcriber_type=self.transcriber_type,
            model_size=self.model_size,
        )

    def _get_gpt(self, model_name: Optional[str], provider_id: Optional[str]) -> GPT:
        """
        根据 provider_id 获取对应的 GPT 实例
        :param model_name: GPT 模型名称
        :param provider_id: 供应商 ID
        :return: GPT 实例
        """
        return build_gpt(model_name, provider_id)

    def _get_downloader(self, platform: str) -> Downloader:
        """
        根据平台名称创建本次任务专用的下载器实例。

        :param platform: 平台标识，需在 SUPPORT_PLATFORM_MAP 中
        :return: 对应的 Downloader 子类实例
        """
        downloader_cls = SUPPORT_PLATFORM_MAP.get(platform)
        logger.debug(f"实例化下载器 -  {platform}")
        if not downloader_cls:
            logger.error(f"不支持的平台：{platform}")
            raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                            message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)
        try:
            instance = downloader_cls()
        except Exception as e:
            logger.error(f"实例化下载器失败：{e}")
            raise
        logger.info(f"使用下载器：{downloader_cls.__name__}")
        return instance

    def _update_status(self, task_id: Optional[str], status: Union[str, TaskStatus],
                       message: Optional[str] = None, extra: Optional[dict] = None):
        """阶段推进：委托 task_state 写状态，并带本 Generator 的 run 抑制规则。"""
        task_state.write_status(
            task_id, status, message=message, extra=extra, run=self._run,
            root=NOTE_OUTPUT_DIR,
        )

    @staticmethod
    def write_task_status(task_id: Optional[str], status: Union[str, TaskStatus],
                          message: Optional[str] = None, extra: Optional[dict] = None) -> None:
        """轻量写状态（不构造 NoteGenerator），调用方负责 run 抑制。"""
        task_state.write_status(task_id, status, message=message, extra=extra,
                                root=NOTE_OUTPUT_DIR)

    def _merge_status_meta(self, task_id: Optional[str], extra: dict) -> None:
        """在不改变当前 status 的前提下并入附加字段。"""
        task_state.merge_status_meta(task_id, extra, root=NOTE_OUTPUT_DIR)

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """把异常转成用户可读且可检索的失败原因（带类型名，替代裸 "Unknown Error"）。"""
        detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
        if isinstance(detail, dict):
            try:
                detail = json.dumps(detail, ensure_ascii=False)
            except Exception:
                detail = str(detail)
        return f"{type(exc).__name__}: {detail}"[:500]

    def _handle_exception(self, task_id, exc):
        logger.error(f"任务异常 (task_id={task_id})", exc_info=True)
        self._update_status(task_id, TaskStatus.FAILED, message=self._format_error(exc))

    @staticmethod
    def _task_scratch_dirs(task_id: Optional[str]) -> Tuple[str, str]:
        """当前任务的帧/拼图临时目录（按 task_id 隔离）。

        并发下必须隔离：VideoReader.run() 会清空并重写 frame_*/grid_* 文件，两个
        任务用共享目录会互删文件、抢占句柄（2026-10-01 任务 4c0b2121 的
        WinError 32 根因）。图片最终以 base64 进请求，目录只作临时区。
        """
        key = task_id or "shared"
        return (
            os.path.join(get_app_dir("output_frames"), key),
            os.path.join(get_app_dir("grid_output"), key),
        )

    def _cleanup_task_scratch(self, task_id: Optional[str]) -> None:
        """任务结束后清理专属帧/拼图目录（best-effort）。"""
        if not task_id:
            return
        for base in ("output_frames", "grid_output"):
            try:
                shutil.rmtree(os.path.join(get_app_dir(base), task_id), ignore_errors=True)
            except Exception:
                pass

    def _adapt_frame_budget(
        self,
        video_path: Path,
        frame_interval: int,
        grid_size: List[int],
        task_id: Optional[str] = None,
    ) -> Tuple[int, List[int]]:
        """按帧预算自适应调整采样参数。

        视频理解失败的头号原因是请求体过大：1 秒间隔 + 1×1 拼图时，20 分钟视频
        会生成 1200 张图、几百 MB 请求，免费上游必挂（2026-10-01 批量失败案例）。
        这里按视频真实时长把「拼图数量」压到 IMAGE_BUDGET 以内、帧数压进
        FRAME_BUDGET。阶梯（2026-10-11 用户定版：逐级升格优先）——兜不住先升
        2×2、再升 3×3，每级都用原间隔试；实在兜不住才拉采样间隔（在最大可用
        格子上算最小拉伸）。时间密度（间隔）比单格分辨率更值得保。
        """
        try:
            import ffmpeg as _ffmpeg
            duration = float(_ffmpeg.probe(str(video_path))["format"]["duration"])
        except Exception as e:
            logger.warning(f"探测视频时长失败，沿用原采样参数: {e}")
            return frame_interval, grid_size
        if duration <= 0:
            return frame_interval, grid_size

        frame_budget = _frame_budget()
        image_budget = _image_budget()
        interval = max(1, int(frame_interval))
        grid = [max(1, int(g)) for g in (grid_size or [2, 2])]
        area = grid[0] * grid[1]

        def images_at(iv: int, a: int) -> float:
            return duration / iv / a

        def over_budget(a: int, iv: int) -> bool:
            return images_at(iv, a) > image_budget or duration / iv > frame_budget

        def required_interval(a: int) -> int:
            """拼图面积 a 下，把拼图压进 IMAGE_BUDGET、帧数压进 FRAME_BUDGET 所需的最小间隔。"""
            iv = interval
            if images_at(iv, a) > image_budget:
                iv = max(iv, math.ceil(duration / (image_budget * a)))
            if duration / iv > frame_budget:
                iv = max(iv, math.ceil(duration / frame_budget))
            return iv

        frames_before = int(duration / interval)
        if not over_budget(area, interval):
            return interval, grid

        # 逐级升格阶梯（用户定版）：2×2 → 3×3 → 拉间隔；格子升到顶仍超预算才动间隔。
        if area < 4 and not over_budget(4, interval):
            grid, area = [2, 2], 4
        elif area < 9 and not over_budget(9, interval):
            grid, area = [3, 3], 9
        else:
            # 间隔兜底：在最大可用格子上算最小拉伸（格子越大 → 需要的拉伸越小）
            if area < 9:
                grid, area = [3, 3], 9
            interval = required_interval(area)

        images_after = int(images_at(interval, area))
        msg = (
            f"已自动调整采样：间隔 {interval}s / 拼图 {grid[0]}×{grid[1]}，"
            f"预计 {images_after} 张拼图（原设置约 {frames_before} 帧，超出预算 "
            f"FRAME_BUDGET={frame_budget}/IMAGE_BUDGET={image_budget}，阶梯 2×2→3×3→拉间隔）"
        )
        logger.info(msg)
        self._update_status(task_id, TaskStatus.DOWNLOADING, message=msg)
        return interval, grid

    def _download_media(
        self,
        downloader: Downloader,
        video_url: Union[str, HttpUrl],
        quality: DownloadQuality,
        audio_cache_file: Path,
        status_phase: TaskStatus,
        platform: str,
        output_path: Optional[str],
        screenshot: bool,
        video_understanding: bool,
        video_interval: int,
        grid_size: List[int],
        skip_audio: bool = False,
        task_id: Optional[str] = None,
    ) -> AudioDownloadResult | None:
        """
        1. 检查音频缓存；若不存在，则按需下载视频（截图/视频理解）与音轨（本地转写）。
        2. 如果需要视频，则先下载视频并生成缩略图集，再（按需）下载音频。
        3. 返回 AudioDownloadResult

        :param downloader: Downloader 实例
        :param video_url: 视频/音频链接
        :param quality: 音频下载质量
        :param audio_cache_file: 本地缓存 JSON 文件路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.DOWNLOADING
        :param platform: 平台标识
        :param output_path: 下载输出目录（可为 None）
        :param screenshot: 是否需要在笔记中插入截图
        :param video_understanding: 是否需要生成缩略图
        :param video_interval: 视频截帧间隔
        :param grid_size: 缩略图网格尺寸
        :param skip_audio: 转写已由平台字幕提供时置 True——只取元信息，不下载音轨
        :return: AudioDownloadResult 对象
        """
        task_id = task_id or audio_cache_file.stem.split("_")[0]
        self._update_status(task_id, status_phase)

        frame_interval = video_interval if video_interval and video_interval > 0 else 6
        need_video = bool(screenshot or video_understanding)

        # 已有缓存，尝试加载
        cached_audio: Optional[AudioDownloadResult] = None
        if audio_cache_file.exists():
            logger.info(f"检测到音频缓存 ({audio_cache_file})，直接读取")
            try:
                data = json.loads(audio_cache_file.read_text(encoding="utf-8"))
                cached_audio = AudioDownloadResult(**data)
                # 缓存命中也补齐视频帧：视频理解开着且本地视频文件还在时重新抽帧。
                # 缓存里没有可用视频（旧缓存来自未开视频理解的运行，或视频文件已
                # 被清理）时**不再静默降级纯文本**——本次请求明确要了视频理解，
                # 继续往下走 need_video 分支重新下载视频再抽帧。2026-10-13 实锤：
                # 旧的"降级为纯文本总结"让开着视频理解的笔记一张截图都没有，
                # 且 *Screenshot-[mm:ss] 标记原样残留正文。
                if video_understanding and grid_size:
                    vp = getattr(cached_audio, "video_path", None)
                    if vp and Path(vp).exists():
                        frame_interval, grid_size = self._adapt_frame_budget(
                            video_path=Path(vp),
                            frame_interval=frame_interval,
                            grid_size=grid_size,
                            task_id=task_id,
                        )
                        self.video_path = Path(vp)
                        self._last_frame_interval = frame_interval
                        self._last_grid_size = list(grid_size)
                        frame_dir, grid_dir = self._task_scratch_dirs(task_id)
                        self.video_img_urls = VideoReader(
                            video_path=vp,
                            grid_size=tuple(grid_size),
                            frame_interval=frame_interval,
                            unit_width=960,
                            unit_height=540,
                            save_quality=80,
                            frame_dir=frame_dir,
                            grid_dir=grid_dir,
                            max_frames=_frame_budget(),
                            cancel_check=self._cancel_requested,
                        ).run()
                        return cached_audio
                    logger.info(
                        f"视频理解开启但缓存无可用本地视频（video_path={vp}），转入视频帧流程重新获取视频"
                    )
                else:
                    return cached_audio
            except Exception as e:
                logger.warning(f"读取音频缓存失败，将重新下载：{e}")
                cached_audio = None

        # 有字幕且不需要截图/视频理解时，只提取元信息不下载文件
        if skip_audio and not need_video:
            logger.info("已有字幕，仅提取视频元信息（不下载音视频）")
            self._check_cancelled()
            return self._fetch_meta_only(downloader, video_url, quality, output_path,
                                         platform, audio_cache_file)

        # 判断是否需要下载视频（截图 / 视频理解）
        if screenshot and not grid_size:
            grid_size = [2, 2]
        if need_video:
            try:
                self._check_cancelled()
                logger.info("开始下载视频")
                video_path_str = downloader.download_video(video_url)
                self.video_path = Path(video_path_str)
                logger.info(f"视频下载完成：{self.video_path}")
                self._check_cancelled()

                if grid_size:
                    frame_interval, grid_size = self._adapt_frame_budget(
                        video_path=self.video_path,
                        frame_interval=frame_interval,
                        grid_size=grid_size,
                        task_id=task_id,
                    )
                    self._last_frame_interval = frame_interval
                    self._last_grid_size = list(grid_size)
                    frame_dir, grid_dir = self._task_scratch_dirs(task_id)
                    self.video_img_urls = VideoReader(
                        video_path=str(self.video_path),
                        grid_size=tuple(grid_size),
                        frame_interval=frame_interval,
                        unit_width=960,
                        unit_height=540,
                        save_quality=80,
                        frame_dir=frame_dir,
                        grid_dir=grid_dir,
                        max_frames=_frame_budget(),
                        cancel_check=self._cancel_requested,
                    ).run()
                else:
                    logger.info("未指定 grid_size，跳过缩略图生成")
            except Exception as exc:
                logger.error(f"视频下载失败：{exc}")
                self._handle_exception(task_id, exc)
                raise

        # 有平台字幕：视频帧（如果开了截图/视频理解）已经拿到，音轨没有任何用途，
        # 这里只补一次元信息就走，不再把整条音轨重下一遍。
        if skip_audio:
            audio = self._fetch_meta_only(
                downloader, video_url, quality, output_path, platform, audio_cache_file
            )
            # 记下本地视频路径：重试命中缓存时能直接复用这份 mp4 抽帧，不必重下
            if self.video_path and Path(self.video_path).exists():
                audio.video_path = str(self.video_path)
                audio_cache_file.write_text(
                    json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return audio

        # 下载音频
        try:
            self._check_cancelled()
            # 视频帧补取走后落到这里（缓存命中但缺视频的重试）：音频文件还在
            # 就直接复用，不必把整条音轨再下一遍
            if (
                cached_audio is not None
                and getattr(cached_audio, "file_path", None)
                and Path(cached_audio.file_path).exists()
            ):
                logger.info("复用缓存音频文件，跳过音频下载")
                audio = cached_audio
                if self.video_path and Path(self.video_path).exists():
                    audio.video_path = str(self.video_path)
                audio_cache_file.write_text(
                    json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return audio
            logger.info("开始下载音频")
            audio = downloader.download(
                video_url=video_url,
                quality=quality,
                output_dir=output_path,
                need_video=need_video,
            )
            if self.video_path and Path(self.video_path).exists():
                audio.video_path = str(self.video_path)
            audio_cache_file.write_text(json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"音频下载并缓存成功 ({audio_cache_file})")
            return audio
        except Exception as exc:
            logger.error(f"音频下载失败：{exc}")
            self._handle_exception(task_id, exc)
            raise

    def _fetch_meta_only(
        self,
        downloader: Downloader,
        video_url: Union[str, HttpUrl],
        quality: DownloadQuality,
        output_path: Optional[str],
        platform: str,
        audio_cache_file: Path,
    ) -> AudioDownloadResult:
        """只取元信息（标题/封面/时长），不落任何媒体文件。

        元信息拿不到不该拖垮整个任务：转写和视频帧都已在手，标题缺了顶多卡片难看，
        所以这里逐级降级到 video_meta 的轻量接口，最后才用占位值。
        """
        audio: Optional[AudioDownloadResult] = None
        try:
            audio = downloader.download(
                video_url=video_url,
                quality=quality,
                output_dir=output_path,
                need_video=False,
                skip_download=True,
            )
        except Exception as exc:
            logger.warning(f"元信息提取失败，改用轻量接口兜底：{exc}")
        if audio is None or not audio.title:
            meta = fetch_video_meta(str(video_url), platform) or {}
            if meta.get("title"):
                audio = AudioDownloadResult(
                    file_path=None,
                    title=meta.get("title"),
                    duration=meta.get("duration") or 0,
                    cover_url=meta.get("cover_url"),
                    platform=platform,
                    video_id=meta.get("video_id"),
                    raw_info={},
                    video_path=None,
                )
                logger.info(f"轻量接口取到元信息：{audio.title}")
        if audio is None:
            logger.warning("元信息全部获取失败，用占位元信息继续（标题/封面将缺失）")
            audio = AudioDownloadResult(
                file_path=None, title=None, duration=0, cover_url=None,
                platform=platform, video_id=None, raw_info={}, video_path=None,
            )
        audio_cache_file.write_text(
            json.dumps(asdict(audio), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"元信息提取完成 ({audio_cache_file})")
        return audio


    def _get_transcript(
        self,
        downloader: Downloader,
        video_url: str,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
        task_id: Optional[str] = None,
    ) -> TranscriptResult | None:
        """
        优先获取平台字幕，没有则 fallback 到音频转写

        :param downloader: 下载器实例
        :param video_url: 视频链接
        :param audio_file: 音频文件路径（用于 fallback 转写）
        :param transcript_cache_file: 缓存文件路径
        :param status_phase: 状态枚举
        :param task_id: 任务 ID
        :return: TranscriptResult 对象
        """
        self._update_status(task_id, status_phase)

        self._check_cancelled()

        # 已有缓存，直接返回
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                return TranscriptResult(language=data.get("language"), full_text=data["full_text"], segments=segments)
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新获取：{e}")

        # 1. 先尝试获取平台字幕
        logger.info("尝试获取平台字幕...")
        try:
            transcript = downloader.download_subtitles(video_url)
            if transcript and transcript.segments:
                logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                # 缓存结果
                transcript_cache_file.write_text(
                    json.dumps(asdict(transcript), ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                return transcript
            else:
                logger.info("平台无可用字幕，将使用音频转写")
        except TaskCancelled:
            raise
        except Exception as e:
            logger.warning(f"获取平台字幕失败: {e}，将使用音频转写")

        # 2. Fallback 到音频转写
        return self._transcribe_audio(
            audio_file=audio_file,
            transcript_cache_file=transcript_cache_file,
            status_phase=status_phase,
            task_id=task_id,
        )

    def _transcribe_audio(
        self,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
        task_id: Optional[str] = None,
    ) -> TranscriptResult | None:
        """
        1. 检查转写缓存；若存在则尝试加载，否则调用转写器生成并缓存。
        2. 返回 TranscriptResult 对象

        :param audio_file: 音频文件本地路径
        :param transcript_cache_file: 转写结果缓存路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.TRANSCRIBING
        :return: TranscriptResult 对象
        """
        task_id = task_id or transcript_cache_file.stem.split("_")[0]
        self._update_status(task_id, status_phase)

        # 有平台字幕时不会下载音轨，转写这条路走不到；真走到了说明字幕也丢了，
        # 与其把 None 丢给 whisper 换一个看不懂的报错，不如直接说清楚。
        if not audio_file:
            raise RuntimeError("没有可用的音频文件（平台无字幕且本次未下载音轨），请重试")

        # 已有缓存，尝试加载
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                # 命中合并缓存：顺手清理段级断点缓存（成功任务不再需要它们）
                cleanup_part_caches(NOTE_OUTPUT_DIR, task_id)
                return TranscriptResult(language=data["language"], full_text=data["full_text"], segments=segments)
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新转写：{e}")

        # 调用转写器（全局信号量：GPU/显存只允许一个 whisper 实例并发，见 ADR-0001）
        # 瞬态网络错误自动重试（2026-10-06）：API 转写引擎（Groq 等）的
        # `Connection error.` 之前一击致命——本地网络/代理抖一下整个任务就失败，
        # 用户得手点三次「重试」。与 universal_gpt 调用重试同款退避节奏；
        # 重试期间持锁，保证「同一时刻只跑一个转写」的不变量不被打破。
        _TRANSCRIBE_RETRIES = 3
        _TRANSCRIBE_RETRY_DELAYS = (3.0, 6.0)
        # 超长音频分段转写：引擎声明了大小上限、原始文件超限、且按下限码率
        # （16kbps→2000B/s）压完仍会超限时，等长硬切 N 段逐段转写再合并——
        # mp3 44.1kHz 的 32k 编码器底码让压缩对 78 分钟以上的音频物理性失败
        # （2026-10-10：262 分钟压到 60.1MB 实锤）。探测失败按不分段兜底，
        # 让既有 fit_size_audio 报出它那句明确的错。
        _max_audio_bytes = getattr(self.transcriber, "max_audio_size_bytes", None)
        use_segmented = bool(
            _max_audio_bytes and audio_file and os.path.exists(audio_file)
            and _should_segment(audio_file, _max_audio_bytes)
        )
        try:
            self._check_cancelled()
            logger.info("开始转写音频（等待转写信号量）")
            # 本地 whisper 独占 GPU 只跑一个；在线引擎（Groq 等）放开并发（2026-10-11）。
            # 等信号量期间保持可取消（_acquire_cancellable），否则「排队等 GPU」
            # 阶段点停止只能干等前一个任务跑完。
            _sem = (
                local_transcribe_semaphore
                if getattr(self, "transcriber_type", "") in LOCAL_TRANSCRIBER_TYPES
                else online_transcribe_semaphore
            )
            self._acquire_cancellable(_sem)
            try:
                transcript: TranscriptResult | None = None
                if use_segmented:

                    def _on_segment_progress(part_index: int, total: int) -> None:
                        self._update_status(
                            task_id, status_phase, message=f"转写中（第 {part_index}/{total} 段）"
                        )

                    transcript = transcribe_segmented(
                        self.transcriber,
                        audio_file,
                        _max_audio_bytes,
                        on_progress=_on_segment_progress,
                        part_cache_dir=NOTE_OUTPUT_DIR,
                        task_id=task_id,
                        should_cancel=self._cancel_requested,
                    )
                else:
                    for attempt in range(1, _TRANSCRIBE_RETRIES + 1):
                        try:
                            self._check_cancelled()
                            logger.info("获得转写信号量，开始转写（第 %d/%d 次尝试）",
                                        attempt, _TRANSCRIBE_RETRIES)
                            transcript = self.transcriber.transcript(file_path=audio_file)
                            break
                        except TaskCancelled:
                            raise
                        except Exception as exc:
                            if attempt >= _TRANSCRIBE_RETRIES:
                                raise
                            delay = _TRANSCRIBE_RETRY_DELAYS[attempt - 1]
                            logger.warning("转写失败（第 %d/%d 次），%.0fs 后重试：%s",
                                           attempt, _TRANSCRIBE_RETRIES, delay, exc)
                            # 退避期间分段睡 + 每段检查：用户对着「一直重试」
                            # 点停止时要立刻生效，不能等退避睡满
                            slept = 0.0
                            while slept < delay:
                                self._check_cancelled()
                                step = min(0.5, delay - slept)
                                time.sleep(step)
                                slept += step
            finally:
                _sem.release()
            transcript_cache_file.write_text(json.dumps(asdict(transcript), ensure_ascii=False, indent=2), encoding="utf-8")
            if use_segmented:
                cleanup_part_caches(NOTE_OUTPUT_DIR, task_id)
            logger.info(f"转写并缓存成功 ({transcript_cache_file})")
            return transcript
        except TaskCancelled:
            raise
        except Exception as exc:
            logger.error(f"音频转写失败：{exc}")
            self._handle_exception(task_id, exc)
            raise

    def _summarize_text(
        self,
        audio_meta: AudioDownloadResult,
        transcript: TranscriptResult,
        gpt: GPT,
        markdown_cache_file: Path,
        link: bool,
        screenshot: bool,
        formats: List[str],
        style: Optional[str],
        extras: Optional[str],
        video_img_urls: List[str],
        task_id: Optional[str] = None,
        video_interval: int = 0,
        grid_size: Optional[List[int]] = None,
    ) -> str | None:
        """调用 GPT 总结转写文本；失败时按「降级阶梯」逐级用更小的请求重试。

        阶梯（2026-10-11 定版：逐级升格优先）——原样（含完整帧）→ 升格 2×2（间隔
        不变，原格子 <2×2 时才有）→ 升格 3×3（间隔不变，<3×3 时才有）→ 减帧
        （间隔×3、格子 3×3 兜底）→ 纯文本。每级内部由 UniversalGPT 自己做网络
        重试；空响应快速失败进入下一级。（见 docs/adr/0003：明确不跨供应商自动切换。）
        """
        task_id = task_id or markdown_cache_file.stem
        self._update_status(task_id, TaskStatus.SUMMARIZING)
        # 质量闸门用的原始转写：长视频只回几十个字时据此判定空壳。
        # getattr 兼容测试里的 SimpleNamespace 转写替身（无 full_text 字段）。
        transcript_text = getattr(transcript, "full_text", "") or "\n".join(
            (getattr(seg, "text", "") or "")
            for seg in getattr(transcript, "segments", [])
        )

        def build_source(imgs: List[str], shot: bool) -> GPTSource:
            return GPTSource(
                title=audio_meta.title,
                segment=transcript.segments,
                tags=audio_meta.raw_info.get("tags", []),
                screenshot=shot,
                video_img_urls=imgs,
                link=link,
                _format=formats,
                style=style,
                extras=extras,
                checkpoint_key=task_id,
            )

        attempts: List[Tuple[str, Optional[List[str]], bool]] = [("full", video_img_urls, screenshot)]
        if video_img_urls:
            # 升格优先阶梯（2026-10-11 定版）：先用原间隔把格子升上去，间隔最后才动。
            _grid = list(self._last_grid_size or [2, 2])
            _area = _grid[0] * _grid[1]
            if _area < 4:
                attempts.append(("grid2", None, screenshot))   # None = 惰性重建（升格 2×2）
            if _area < 9:
                attempts.append(("grid3", None, screenshot))   # 惰性重建（升格 3×3）
            attempts.append(("thinned", None, screenshot))     # 惰性重建（间隔×3、3×3 兜底）
        attempts.append(("text_only", [], False))

        last_exc: Optional[Exception] = None
        # 阶梯总死线（2026-10-06「总结中长期卡在总结中」反馈）：单次调用由
        # 流式空闲超时兜底，但三级阶梯串起来最坏可拖 20-30 分钟且 UI 零反馈，
        # 看起来像卡死。超过死线就明确失败并写清原因，交给用户重试（有缓存，重试很快）。
        deadline_seconds = float(os.getenv("SUMMARIZE_LADDER_DEADLINE_SECONDS", "1500"))
        # 分批喂图后长视频一次总结可能有几十个批次的模型调用，固定 1500s 必然
        # 误杀：按预计批次数线性放宽（每批 +240s；设 0 可关掉放宽回到固定死线）。
        est_chunks = max(1, math.ceil(len(video_img_urls) / 20)) if video_img_urls else 1
        per_chunk_seconds = float(os.getenv("SUMMARIZE_LADDER_DEADLINE_PER_CHUNK", "240"))
        deadline_seconds += max(0, est_chunks - 1) * per_chunk_seconds
        ladder_started_at = time.monotonic()
        strategy_labels = {
            "full": "原样（含视频帧）",
            "grid2": "升格 2×2",
            "grid3": "升格 3×3",
            "thinned": "减帧（间隔×3）",
            "text_only": "纯文本",
        }
        for idx, (strategy, imgs, shot) in enumerate(attempts):
            # 降级阶梯的每一级之间检查取消：用户在「上游不稳定、正在用 X 策略
            # 重试」时点停止，不该再陪跑剩下的层级
            self._check_cancelled()
            if strategy in ("grid2", "grid3", "thinned"):
                imgs = self._rebuild_video_grids(task_id, mode=strategy)
                if not imgs:
                    logger.warning(f"拼图重建失败（{strategy}，无可用视频文件），跳过该级")
                    continue
            if idx > 0:
                label = strategy_labels.get(strategy, strategy)
                cause = type(last_exc).__name__ if last_exc else "未知错误"
                logger.warning(f"总结失败进入降级：策略={label} 原因={cause} (task_id={task_id})")
                elapsed_min = (time.monotonic() - ladder_started_at) / 60
                if elapsed_min * 60 > deadline_seconds:
                    deadline_exc = RuntimeError(
                        f"总结累计 {elapsed_min:.0f} 分钟仍未成功（上游模型响应过慢），已停止降级重试；请稍后重试或更换模型"
                    )
                    self._handle_exception(task_id, deadline_exc)
                    raise deadline_exc
                self._update_status(
                    task_id, TaskStatus.SUMMARIZING,
                    message=f"上游不稳定（{cause}），正在用「{label}」策略重试（第 {idx + 1}/{len(attempts)} 次）",
                )
            else:
                self._update_status(
                    task_id, TaskStatus.SUMMARIZING,
                    message=f"正在调用模型总结（{strategy_labels.get(strategy, strategy)}），长视频可能需要几分钟",
                )
            try:
                markdown = gpt.summarize(build_source(imgs or [], shot))
                # 上游对敏感内容可能返回 HTTP 200 + 「抱歉，这个问题未找到相关
                # 结果。」（2026-10-21 实拍）。不校验就会把空壳笔记标成成功。
                # 拒答属于内容层面的确定性失败，换更小的请求重发同一段内容
                # 没有意义，直接终止降级阶梯，让用户换模型重试。
                ensure_note_content(markdown, transcript_text)
                markdown_cache_file.write_text(markdown, encoding="utf-8")
                logger.info(f"GPT 总结并缓存成功 ({markdown_cache_file}, 策略={strategy})")
                return markdown
            except TaskCancelled:
                # 用户停止：不许被当成「这一级失败」卷进下一级降级
                raise
            except NoteContentError as exc:
                last_exc = exc
                logger.error(f"GPT 总结内容无效（策略={strategy}）：{exc}")
                if exc.fatal:
                    break
            except Exception as exc:
                last_exc = exc
                logger.error(f"GPT 总结失败（策略={strategy}）：{type(exc).__name__}: {exc}")

        if last_exc is not None:
            self._handle_exception(task_id, last_exc)
            raise last_exc
        raise RuntimeError("总结失败：所有降级策略均未成功")

    def _rebuild_video_grids(self, task_id: Optional[str] = None, mode: str = "thinned") -> List[str]:
        """总结降级的拼图重建（升格优先阶梯，2026-10-11 定版）。

        grid2：升格 2×2、间隔不变；grid3：升格 3×3、间隔不变（间隔都没动，帧
        缓存可原样复用，只重新拼图）；thinned：间隔×3、格子升到 3×3 兜底。
        """
        try:
            if not self.video_path or not Path(self.video_path).exists():
                return []
            base_interval = self._last_frame_interval or 6
            grid = list(self._last_grid_size or [2, 2])
            area = grid[0] * grid[1] if grid else 4
            if mode == "grid2":
                grid, interval, reuse = [2, 2], base_interval, True
            elif mode == "grid3":
                grid, interval, reuse = [3, 3], base_interval, True
            else:  # thinned：间隔变了，帧必须重新抽
                grid = [3, 3] if area < 9 else grid
                interval = max(1, base_interval * 3)
                reuse = False
            logger.info(f"降级重建（{mode}）：interval {base_interval}->{interval}, grid={grid}, 复用帧缓存={reuse}")
            frame_dir, grid_dir = self._task_scratch_dirs(task_id)
            return VideoReader(
                video_path=str(self.video_path),
                grid_size=tuple(grid),
                frame_interval=interval,
                unit_width=960,
                unit_height=540,
                save_quality=80,
                frame_dir=frame_dir,
                grid_dir=grid_dir,
                max_frames=_frame_budget(),
                cancel_check=self._cancel_requested,
            ).run(reuse_frames=reuse)
        except TaskCancelled:
            raise
        except Exception as e:
            logger.warning(f"拼图重建失败: {e}")
            return []

    def _post_process_markdown(
        self,
        markdown: str,
        video_path: Optional[Path],
        formats: List[str],
        audio_meta: AudioDownloadResult,
        platform: str,
    ) -> str:
        """
        对生成的 Markdown 做后期处理：插入截图和/或插入链接。

        :param markdown: 原始 Markdown 字符串
        :param video_path: 本地视频路径（可为 None）
        :param formats: 包含 'link' 或 'screenshot' 的列表
        :param audio_meta: AudioDownloadResult 元信息，用于链接替换
        :param platform: 平台标识，用于链接替换
        :return: 处理后的 Markdown 字符串
        """
        if "screenshot" in formats:
            if video_path:
                try:
                    markdown = self._insert_screenshots(markdown, video_path)
                except Exception as exc:
                    logger.warning("截图插入失败，跳过该步骤")
            else:
                logger.warning("已选截图格式但没有可用的本地视频（视频理解被降级），移除未兑现的截图标记")
            # 兜底清扫：无论插入是整段跳过还是个别失败，Screenshot 标记一律不许
            # 留在正文（2026-10-13 用户实锤：抽帧没跑成时 6 行 *Screenshot-[mm:ss]
            # 原样出现在笔记里且一张图都没有）
            markdown = strip_screenshot_markers(markdown)

        if "link" in formats:
            try:
                markdown = replace_content_markers(markdown, video_id=audio_meta.video_id, platform=platform)
            except Exception as e:
                logger.warning(f"链接插入失败，跳过该步骤：{e}")

        return cleanup_marker_residue(markdown)

    def _insert_screenshots(self, markdown: str, video_path: Path) -> str | None | Any:
        """
        扫描 Markdown 文本中所有 Screenshot 标记，并替换为实际生成的截图链接。

        :param markdown: 含有 *Screenshot-mm:ss 或 Screenshot-[mm:ss] 标记的 Markdown 文本
        :param video_path: 本地视频文件路径
        :return: 替换后的 Markdown 字符串
        """
        matches: List[Tuple[str, int]] = extract_screenshot_timestamps(markdown)
        failed = 0
        for idx, (marker, ts) in enumerate(matches):
            try:
                img_path = generate_screenshot(str(video_path), str(IMAGE_OUTPUT_DIR), ts, idx)
                filename = Path(img_path).name
                # 构建前端可访问的 URL，例如 /static/screenshots/{filename}
                img_url = f"{IMAGE_BASE_URL.rstrip('/')}/{filename}"
                markdown = markdown.replace(marker, f"![]({img_url})", 1)
            except Exception as exc:
                # 单张截图失败不再毁掉整篇笔记（旧行为 return None 会让上游
                # 把整个 markdown 置空，笔记直接"生成成功"却空白）。改为：
                # 记日志 + 把标记从正文里摘掉（留着一行 *Screenshot-[mm:ss]
                # 是纯噪音），继续处理其余标记。
                failed += 1
                logger.warning(f"生成截图失败，已移除该标记 (timestamp={ts})：{exc}")
                markdown = markdown.replace(marker, "", 1)
        if failed:
            logger.warning(f"截图插入完成，{failed}/{len(matches)} 张失败被跳过")
        return markdown

    def _save_metadata(self, video_id: str, platform: str, task_id: str) -> None:
        """
        将生成的笔记任务记录插入数据库

        :param video_id: 视频 ID
        :param platform: 平台标识
        :param task_id: 任务 ID
        """
        try:
            insert_video_task(video_id=video_id, platform=platform, task_id=task_id)
            logger.info(f"已保存任务记录到数据库 (video_id={video_id}, platform={platform}, task_id={task_id})")
        except Exception as e:
            logger.error(f"保存任务记录失败：{e}")


# ---------------- 模块级辅助（供路由 / 自动化复用） ----------------

def write_task_status(task_id: str, status: Union[str, TaskStatus],
                     message: Optional[str] = None, extra: Optional[dict] = None) -> None:
    """供路由/脚本用的轻量状态写入（不构造 NoteGenerator）。"""
    task_state.write_status(task_id, status, message=message, extra=extra,
                            root=NOTE_OUTPUT_DIR)


def build_gpt(model_name: Optional[str], provider_id: Optional[str]) -> GPT:
    """按供应商 + 模型名构建 GPT 实例（NoteGenerator 与存量笔记重整理共用）。"""
    provider = ProviderService.get_provider_by_id(provider_id)
    if not provider:
        logger.error(f"[build_gpt] 未找到模型供应商: provider_id={provider_id}")
        raise ProviderError(code=ProviderErrorEnum.NOT_FOUND, message=ProviderErrorEnum.NOT_FOUND.message)
    logger.info(f"创建 GPT 实例 {provider_id}")
    # 思考强度是模型级属性（不同模型档位不同），从 models 行读取；
    # 查不到模型行（异常库等边缘场景）时按未设置处理，不传参。
    model_effort = None
    try:
        _model_row = get_model_by_provider_and_name(provider["id"], model_name or "")
        if _model_row:
            model_effort = _model_row.get("thinking_effort") or None
    except Exception as e:
        logger.warning(f"[build_gpt] 读取模型思考强度失败（按未设置处理）: {e}")
    config = ModelConfig(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model_name=model_name,
        provider=provider["type"],
        name=provider["name"],
        api_format=provider.get("api_format") or "chat",
        extra_headers=provider.get("extra_headers"),
        thinking_effort=model_effort,
    )
    return GPTFactory().from_config(config)


def refinalize_stored_note(
    task_id: str,
    model_name: Optional[str] = None,
    provider_id: Optional[str] = None,
    formats: Optional[list] = None,
) -> dict:
    """对已生成的笔记重跑「合并后整理」：摘掉目录/总结残留 → 重建目录 → 重写 AI 总结。

    只读结果文件 + 一次纯文本模型调用（不重新下载/转写/喂图），几秒到一分钟完成。
    用于把存量笔记修复到新版整理逻辑（2026-10-14：合并阶段产出的
    「本段/本批」堆叠式总结与目录残留，无需让用户重新生成整条笔记）。
    默认沿用结果文件里记录的模型；调用方可显式指定（原模型不可用时换用可用模型）。
    """
    task_id = (task_id or "").strip()
    result_path = NOTE_OUTPUT_DIR / f"{task_id}.json"
    if not task_id or not result_path.exists():
        raise FileNotFoundError(f"结果文件不存在：{task_id}.json")
    data = json.loads(result_path.read_text(encoding="utf-8"))
    md = data.get("markdown") or ""
    if isinstance(md, list):  # 兼容多版本文档形态
        md = ((md[0] or {}).get("content", "") if md else "")
    if not isinstance(md, str) or not md.strip():
        raise FileNotFoundError("结果文件里没有可整理的 markdown 正文")

    fmt = list(formats or data.get("format") or [])
    if not fmt:
        # 老结果文件没记录格式：按正文实际带没带这两段推断
        if _re.search(r"(?m)^#{1,6}\s*目录", md):
            fmt.append("toc")
        if _re.search(r"(?m)^#{1,6}\s*AI\s*总结", md):
            fmt.append("summary")

    model = model_name or data.get("model_name")
    provider = provider_id or data.get("provider_id")
    if not model or not provider:
        raise ValueError("结果文件缺少 model_name/provider_id，需在请求里显式提供")

    gpt = build_gpt(model, provider)
    gpt._format = fmt
    # 是否保留 *Content 标记形态取决于正文是否已有跳转链接（存量笔记都已转换过）
    gpt.link = "[原片 @" in md
    new_md = gpt.finalize_note(md) if hasattr(gpt, "finalize_note") else md
    new_md = cleanup_marker_residue(normalize_math_delimiters(new_md) or new_md)

    data["markdown"] = new_md
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(result_path)
    # markdown 缓存文件同步（与生成流程落盘的位置一致）
    (NOTE_OUTPUT_DIR / f"{task_id}_markdown.md").write_text(new_md, encoding="utf-8")
    # 触碰状态文件 mtime：前端 /tasks/recent 同步钩子的 updated_at 靠它，
    # 不触碰的话内容变了前端也感知不到、要等下次别的事件才会刷新
    status_path = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
    if status_path.exists():
        try:
            os.utime(status_path, None)
        except OSError:
            pass
    logger.info(
        f"存量笔记重整理完成 (task_id={task_id}, formats={fmt}, {len(md)} → {len(new_md)} 字符)"
    )
    return {
        "task_id": task_id,
        "model_name": model,
        "provider_id": provider,
        "formats": fmt,
        "markdown": new_md,
    }


def find_active_task_by_video(video_id: str) -> Optional[dict]:
    """查找同一 video_id 的未终态任务（提交去重用）。"""
    return task_state.find_active_task_by_video(video_id, root=NOTE_OUTPUT_DIR)


def list_recent_tasks(limit: int = 80) -> List[dict]:
    """按最近更新倒序返回任务概要列表（供 /tasks/recent 增量同步）。"""
    return task_state.list_recent_tasks(limit=limit, root=NOTE_OUTPUT_DIR)


# 运行中任务登记（活跃表 / run / 取消墓碑）已移到 app.services.task_state.runs；
# 本文件保留同名导入，供 router 与历史调用点继续 from app.services.note import。


def release_stale_active_task(task_id: str) -> bool:
    """内存登记 vs 磁盘终态对齐；root 跟随本模块的 NOTE_OUTPUT_DIR。"""
    return task_state.release_stale_active_task(task_id, root=NOTE_OUTPUT_DIR)


# ---------------- 任务删除 / 中断收敛 ----------------

def find_task_ids_by_video(video_id: str, platform: Optional[str] = None) -> List[str]:
    """按 video_id 找任务（老前端只传 video_id 时用）。"""
    return task_state.find_task_ids_by_video(video_id, platform, root=NOTE_OUTPUT_DIR)


def purge_task(task_id: str) -> dict:
    """真正删除一个任务：状态文件 + 缓存 + 导出结果 + 向量索引。"""
    return task_state.purge_task(task_id, root=NOTE_OUTPUT_DIR)


def reap_interrupted_tasks(process_started_at: float) -> int:
    """把上一个进程遗留的非终态任务标记为失败。"""
    return task_state.reap_interrupted_tasks(process_started_at, root=NOTE_OUTPUT_DIR)

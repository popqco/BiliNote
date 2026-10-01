import json
import logging
import math
import os
import shutil
import threading
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
from app.enmus.exception import NoteErrorEnum, ProviderErrorEnum
from app.enmus.task_status_enums import TaskStatus
from app.enmus.note_enums import DownloadQuality
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.gpt.base import GPT
from app.gpt.gpt_factory import GPTFactory
from app.models.audio_model import AudioDownloadResult
from app.models.gpt_model import GPTSource
from app.models.model_config import ModelConfig
from app.models.notes_model import AudioDownloadResult, NoteResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.constant import SUPPORT_PLATFORM_MAP
from app.services.provider import ProviderService
from app.services.task_serial_executor import transcribe_semaphore
from app.services.video_meta import fetch_video_meta
from app.transcriber.base import Transcriber
from app.transcriber.transcriber_provider import get_transcriber, _transcribers
from app.utils.note_helper import replace_content_markers, prepend_source_link, normalize_math_delimiters
from app.utils.logger import get_logger
from app.utils.path_helper import get_app_dir
from app.utils.screenshot_marker import extract_screenshot_timestamps
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
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_OUTPUT_DIR = os.getenv("OUT_DIR", "./static/screenshots")
# 图片基础 URL（用于生成 Markdown 中的图片链接，需前端静态目录对应）
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "/static/screenshots")

# 日志配置：必须用统一 get_logger（会挂 logs/app.log 的文件 handler）。
# 裸 logging.getLogger 没有 handler，记录会全部丢失——2026-10-01 排查总结阶段
# 「日志消失」时确认的根因。
logger = get_logger(__name__)

# 视频理解帧预算（见 _adapt_frame_budget）：超过时自动拉大间隔/加大拼图
FRAME_BUDGET = 600   # 抽帧总数上限
IMAGE_BUDGET = 150   # 拼图（每张拼图 = 1 个 image 块）数量上限

# 非终态判定用（提交去重 / 任务列表）
TERMINAL_STATUSES = {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value}


class NoteGenerator:
    """
    NoteGenerator 用于执行视频/音频下载、转写、GPT 生成笔记、插入截图/链接、
    以及将任务信息写入状态文件与数据库等功能。
    """

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
        logger.info("NoteGenerator 初始化完成")


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
    ) -> NoteResult | None:
        """
        主流程：按步骤依次下载、转写、GPT 总结、截图/链接处理、存库、返回 NoteResult。

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

        try:
            logger.info(f"开始生成笔记 (task_id={task_id})")
            self._update_status(task_id, TaskStatus.PARSING)

            # 获取下载器与 GPT 实例

            downloader = self._get_downloader(platform)
            gpt = self._get_gpt(model_name, provider_id)

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

            # 5. 保存记录到数据库
            self._update_status(task_id, TaskStatus.SAVING)
            self._save_metadata(video_id=audio_meta.video_id, platform=platform, task_id=task_id)

            # 6. 完成
            self._update_status(task_id, TaskStatus.SUCCESS)
            logger.info(f"笔记生成成功 (task_id={task_id})")
            return NoteResult(markdown=markdown, transcript=transcript, audio_meta=audio_meta)

        except Exception as exc:
            logger.error(f"生成笔记流程异常 (task_id={task_id})：{exc}", exc_info=True)
            self._update_status(task_id, TaskStatus.FAILED, message=self._format_error(exc))
            return None
        finally:
            # 清理任务专属的帧/拼图临时目录（并发隔离方案的配套清理）
            self._cleanup_task_scratch(task_id)

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
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            logger.error(f"[get_gpt] 未找到模型供应商: provider_id={provider_id}")
            raise ProviderError(code=ProviderErrorEnum.NOT_FOUND,message=ProviderErrorEnum.NOT_FOUND.message)
        logger.info(f"创建 GPT 实例 {provider_id}")
        config = ModelConfig(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            model_name=model_name,
            provider=provider["type"],
            name=provider["name"],
        )
        return GPTFactory().from_config(config)

    def _get_downloader(self, platform: str) -> Downloader:
        """
        根据平台名称获取对应的下载器实例

        :param platform: 平台标识，需在 SUPPORT_PLATFORM_MAP 中
        :return: 对应的 Downloader 子类实例
        """
        downloader_cls = SUPPORT_PLATFORM_MAP.get(platform)
        logger.debug(f"实例化下载器 -  {platform}")
        instance = None
        if not downloader_cls:
            logger.error(f"不支持的平台：{platform}")
            raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                            message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)
        try:
            instance = downloader_cls
        except Exception as e:
            logger.error(f"实例化下载器失败：{e}")


        logger.info(f"使用下载器：{downloader_cls.__class__}")
        return instance

    def _update_status(self, task_id: Optional[str], status: Union[str, TaskStatus],
                       message: Optional[str] = None, extra: Optional[dict] = None):
        """
        创建或更新 {task_id}.status.json，记录当前任务状态。

        合并式写入：保留文件中已有的附加字段（video_id/origin/video_url/audio_meta），
        只覆盖 status/message/extra——阶段推进不会丢掉提交时写入的元数据。

        :param task_id: 任务唯一 ID
        :param status: TaskStatus 枚举或自定义状态字符串
        :param message: 可选消息；为空时清除上一阶段的 message
        :param extra: 附加字段，与状态一起原子写入
        """
        if not task_id:
            return
        status_file = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
        data: dict = {}
        try:
            if status_file.exists():
                data = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["status"] = status.value if isinstance(status, TaskStatus) else status
        if message:
            data["message"] = message
        else:
            data.pop("message", None)
        if extra:
            data.update(extra)
        self._write_status_file(task_id, data)

    def _write_status_file(self, task_id: str, data: dict) -> None:
        """原子写 {task_id}.status.json（tmp + rename）。"""
        NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        status_file = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
        try:
            temp_file = status_file.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(status_file)
        except Exception as e:
            logger.error(f"写入状态文件失败 (task_id={task_id})：{e}")
            try:
                with status_file.open("w", encoding="utf-8") as f:
                    f.write(f"Error writing status: {str(e)}")
            except Exception:
                logger.error(f"写入错误 {e}")

    def _merge_status_meta(self, task_id: Optional[str], extra: dict) -> None:
        """在不改变当前 status 的前提下，把附加字段并入 {task_id}.status.json。"""
        if not task_id:
            return
        status_file = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
        data: dict = {}
        try:
            if status_file.exists():
                data = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data.setdefault("status", TaskStatus.PENDING.value)
        data.update(extra)
        self._write_status_file(task_id, data)

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
        这里按视频真实时长把「拼图数量」压到 IMAGE_BUDGET 以内：优先加大拼图
        （4/9 帧拼一张，不降低覆盖密度），还不够再拉大采样间隔。
        """
        try:
            import ffmpeg as _ffmpeg
            duration = float(_ffmpeg.probe(str(video_path))["format"]["duration"])
        except Exception as e:
            logger.warning(f"探测视频时长失败，沿用原采样参数: {e}")
            return frame_interval, grid_size
        if duration <= 0:
            return frame_interval, grid_size

        interval = max(1, int(frame_interval))
        grid = [max(1, int(g)) for g in (grid_size or [2, 2])]
        area = grid[0] * grid[1]

        def images_at(iv: int, a: int) -> float:
            return duration / iv / a

        frames_before = int(duration / interval)
        if images_at(interval, area) <= IMAGE_BUDGET and frames_before <= FRAME_BUDGET:
            return interval, grid

        # 1) 先加大拼图（最多 3×3），不改变抽帧密度
        while area < 9 and images_at(interval, area) > IMAGE_BUDGET:
            side = int(area ** 0.5) + 1
            grid = [side, side]
            area = grid[0] * grid[1]
        # 2) 还不够就拉大采样间隔
        if images_at(interval, area) > IMAGE_BUDGET:
            interval = max(interval, math.ceil(duration / (IMAGE_BUDGET * area)))
        if frames_before > FRAME_BUDGET:
            interval = max(interval, math.ceil(duration / FRAME_BUDGET))

        images_after = int(images_at(interval, area))
        msg = (
            f"已自动调整采样：间隔 {interval}s / 拼图 {grid[0]}×{grid[1]}，"
            f"预计 {images_after} 张拼图（原设置约 {frames_before} 帧，超出上游稳定承受范围）"
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
        if audio_cache_file.exists():
            logger.info(f"检测到音频缓存 ({audio_cache_file})，直接读取")
            try:
                data = json.loads(audio_cache_file.read_text(encoding="utf-8"))
                audio = AudioDownloadResult(**data)
                # 缓存命中也补齐视频帧：视频理解开着且本地视频文件还在时重新抽帧；
                # 视频文件已不在则自动按纯文本继续（降级链的下限兜底）
                if video_understanding and grid_size:
                    vp = getattr(audio, "video_path", None)
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
                        ).run()
                    else:
                        logger.warning(f"视频文件不在本地（{vp}），视频理解降级为纯文本总结")
                return audio
            except Exception as e:
                logger.warning(f"读取音频缓存失败，将重新下载：{e}")

        # 有字幕且不需要截图/视频理解时，只提取元信息不下载文件
        if skip_audio and not need_video:
            logger.info("已有字幕，仅提取视频元信息（不下载音视频）")
            return self._fetch_meta_only(downloader, video_url, quality, output_path,
                                         platform, audio_cache_file)

        # 判断是否需要下载视频（截图 / 视频理解）
        if screenshot and not grid_size:
            grid_size = [2, 2]
        if need_video:
            try:
                logger.info("开始下载视频")
                video_path_str = downloader.download_video(video_url)
                self.video_path = Path(video_path_str)
                logger.info(f"视频下载完成：{self.video_path}")

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
                return TranscriptResult(language=data["language"], full_text=data["full_text"], segments=segments)
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新转写：{e}")

        # 调用转写器（全局信号量：GPU/显存只允许一个 whisper 实例并发，见 ADR-0001）
        try:
            logger.info("开始转写音频（等待转写信号量）")
            with transcribe_semaphore:
                logger.info("获得转写信号量，开始转写")
                transcript = self.transcriber.transcript(file_path=audio_file)
            transcript_cache_file.write_text(json.dumps(asdict(transcript), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"转写并缓存成功 ({transcript_cache_file})")
            return transcript
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

        阶梯：原样（含完整帧）→ 减帧（间隔×3、拼图至少 2×2）→ 纯文本。
        每一级内部由 UniversalGPT 自己做网络重试；空响应快速失败进入下一级。
        （见 docs/adr/0003：明确不跨供应商自动切换。）
        """
        task_id = task_id or markdown_cache_file.stem
        self._update_status(task_id, TaskStatus.SUMMARIZING)

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
            attempts.append(("thinned", None, screenshot))  # None = 惰性重建减帧拼图
        attempts.append(("text_only", [], False))

        last_exc: Optional[Exception] = None
        for idx, (strategy, imgs, shot) in enumerate(attempts):
            if strategy == "thinned":
                imgs = self._rebuild_video_grids(task_id)
                if not imgs:
                    logger.warning("减帧重建失败（无可用视频文件），跳过该级")
                    continue
            if idx > 0:
                label = {"thinned": "减帧", "text_only": "纯文本"}.get(strategy, strategy)
                logger.warning(f"总结失败进入降级：策略={label} (task_id={task_id})")
                self._update_status(
                    task_id, TaskStatus.SUMMARIZING,
                    message=f"上游不稳定，正在用「{label}」策略重试（第 {idx + 1}/{len(attempts)} 次）",
                )
            try:
                markdown = gpt.summarize(build_source(imgs or [], shot))
                markdown_cache_file.write_text(markdown, encoding="utf-8")
                logger.info(f"GPT 总结并缓存成功 ({markdown_cache_file}, 策略={strategy})")
                return markdown
            except Exception as exc:
                last_exc = exc
                logger.error(f"GPT 总结失败（策略={strategy}）：{type(exc).__name__}: {exc}")

        if last_exc is not None:
            self._handle_exception(task_id, last_exc)
            raise last_exc
        raise RuntimeError("总结失败：所有降级策略均未成功")

    def _rebuild_video_grids(self, task_id: Optional[str] = None) -> List[str]:
        """降级第 2 级：用「间隔×3、拼图至少 2×2」重建缩略图网格（请求体积约 1/3）。"""
        try:
            if not self.video_path or not Path(self.video_path).exists():
                return []
            base_interval = self._last_frame_interval or 6
            grid = list(self._last_grid_size or [2, 2])
            if grid[0] * grid[1] < 4:
                grid = [2, 2]
            thinned_interval = max(1, base_interval * 3)
            logger.info(f"减帧重建：interval {base_interval}->{thinned_interval}, grid={grid}")
            frame_dir, grid_dir = self._task_scratch_dirs(task_id)
            return VideoReader(
                video_path=str(self.video_path),
                grid_size=tuple(grid),
                frame_interval=thinned_interval,
                unit_width=960,
                unit_height=540,
                save_quality=80,
                frame_dir=frame_dir,
                grid_dir=grid_dir,
            ).run()
        except Exception as e:
            logger.warning(f"减帧重建失败: {e}")
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
        if "screenshot" in formats and video_path:
            try:
                markdown = self._insert_screenshots(markdown, video_path)
            except Exception as exc:
                logger.warning("截图插入失败，跳过该步骤")

        if "link" in formats:
            try:
                markdown = replace_content_markers(markdown, video_id=audio_meta.video_id, platform=platform)
            except Exception as e:
                logger.warning(f"链接插入失败，跳过该步骤：{e}")

        return markdown

    def _insert_screenshots(self, markdown: str, video_path: Path) -> str | None | Any:
        """
        扫描 Markdown 文本中所有 Screenshot 标记，并替换为实际生成的截图链接。

        :param markdown: 含有 *Screenshot-mm:ss 或 Screenshot-[mm:ss] 标记的 Markdown 文本
        :param video_path: 本地视频文件路径
        :return: 替换后的 Markdown 字符串
        """
        matches: List[Tuple[str, int]] = extract_screenshot_timestamps(markdown)
        for idx, (marker, ts) in enumerate(matches):
            try:
                img_path = generate_screenshot(str(video_path), str(IMAGE_OUTPUT_DIR), ts, idx)
                filename = Path(img_path).name
                # 构建前端可访问的 URL，例如 /static/screenshots/{filename}
                img_url = f"{IMAGE_BASE_URL.rstrip('/')}/{filename}"
                markdown = markdown.replace(marker, f"![]({img_url})", 1)
            except Exception as exc:
                logger.error(f"生成截图失败 (timestamp={ts})：{exc}")
                # self._handle_exception(task_id, exc)
                return None
        return markdown

    @staticmethod
    def _extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
        """
        从 Markdown 文本中提取所有 '*Screenshot-mm:ss' 或 'Screenshot-[mm:ss]' 标记，
        返回 [(原始标记文本, 时间戳秒数), ...] 列表。

        :param markdown: 原始 Markdown 文本
        :return: 标记与对应时间戳秒数的列表
        """
        return extract_screenshot_timestamps(markdown)

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

def find_active_task_by_video(video_id: str) -> Optional[dict]:
    """查找同一 video_id 的未终态任务（提交去重用）。

    状态文件是任务的唯一事实源；video_id 在提交时即写入状态文件
    （routers.note.generate_note）。返回 {"task_id","status"} 或 None。
    """
    if not video_id:
        return None
    try:
        candidates = sorted(
            NOTE_OUTPUT_DIR.glob("*.status.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for f in candidates:
        stem = f.name[: -len(".status.json")]
        if "_" in stem:
            continue  # 过滤历史遗留的 {task_id}_markdown.status.json
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("video_id") != video_id:
            continue
        if data.get("status") in TERMINAL_STATUSES:
            continue
        return {"task_id": stem, "status": data.get("status")}
    return None


def list_recent_tasks(limit: int = 80) -> List[dict]:
    """按最近更新倒序返回任务概要列表（供 /tasks/recent 增量同步）。

    只保留 {uuid}.status.json 主文件，字段与前端任务卡片所需对齐。
    """
    items: List[dict] = []
    try:
        files = sorted(
            NOTE_OUTPUT_DIR.glob("*.status.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return items
    for f in files:
        stem = f.name[: -len(".status.json")]
        if "_" in stem:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("audio_meta") or {}
        try:
            updated_at = int(f.stat().st_mtime)
        except OSError:
            updated_at = 0
        items.append({
            "task_id": stem,
            "status": data.get("status"),
            "message": data.get("message"),
            "video_id": data.get("video_id") or meta.get("video_id"),
            "platform": data.get("platform") or meta.get("platform"),
            "video_url": data.get("video_url"),
            "origin": data.get("origin", "manual"),
            "title": meta.get("title"),
            "cover_url": meta.get("cover_url"),
            "duration": meta.get("duration"),
            "updated_at": updated_at,
            "has_result": (NOTE_OUTPUT_DIR / f"{stem}.json").exists(),
        })
        if len(items) >= limit:
            break
    return items


# ---------------- 运行中任务登记表（进程内） ----------------
# 作用有两个：
#   1) 拦截「同一个任务在跑的时候又被点一次重新生成」——否则第二次提交会把
#      正在跑的任务状态文件改写成 PENDING，前端就一直显示「排队中」直到前一次
#      真正跑完（2026-10-01 实测复现）。
#   2) 删除任务时判断是否正在生成，正在跑的不允许删（否则状态文件会被下一次
#      _update_status 重新写回来）。
# 只对本进程可见：进程重启后的残留任务由 reap_interrupted_tasks() 收敛。

_active_tasks: dict[str, str] = {}          # task_id -> video_id
_active_tasks_lock = threading.Lock()


def mark_task_active(task_id: str, video_id: Optional[str] = None) -> None:
    if not task_id:
        return
    with _active_tasks_lock:
        _active_tasks[task_id] = video_id or ""


def mark_task_done(task_id: str) -> None:
    if not task_id:
        return
    with _active_tasks_lock:
        _active_tasks.pop(task_id, None)


def is_task_active(task_id: str) -> bool:
    with _active_tasks_lock:
        return task_id in _active_tasks


def active_task_for_video(video_id: str) -> Optional[str]:
    """同视频是否已有任务在本进程排队/执行中（跨进程靠状态文件兜底）。"""
    if not video_id:
        return None
    with _active_tasks_lock:
        for tid, vid in _active_tasks.items():
            if vid and vid == video_id:
                return tid
    return None


# ---------------- 任务删除 / 中断收敛 ----------------

_TASK_ARTIFACT_SUFFIXES = (
    ".status.json",
    ".json",
    "_audio.json",
    "_transcript.json",
    "_markdown.md",
    "_markdown.status.json",
    "_markdown.gpt.checkpoint.json",
)


def _read_status_file(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_task_ids_by_video(video_id: str, platform: Optional[str] = None) -> List[str]:
    """按 video_id 找任务（老前端只传 video_id 时用）。"""
    if not video_id:
        return []
    found: List[str] = []
    for f in NOTE_OUTPUT_DIR.glob("*.status.json"):
        stem = f.name[: -len(".status.json")]
        if "_" in stem:
            continue
        data = _read_status_file(f) or {}
        if data.get("video_id") != video_id:
            continue
        if platform and data.get("platform") and data.get("platform") != platform:
            continue
        found.append(stem)
    return found


def purge_task(task_id: str) -> dict:
    """真正删除一个任务：状态文件 + 缓存 + 导出结果 + 向量索引。

    只删 {task_id} 前缀的文件——同一视频的多份笔记各有独立 task_id，互不牵连。
    数据库里的 video→task 记录仅在「该视频已无任何任务文件」时才清，避免误伤
    同一视频的另一份笔记。
    """
    if not task_id:
        return {"deleted": 0, "files": []}

    # 先读状态（要等文件删完就来不及了）：video_id / platform 用于后面判断
    # 是否连数据库记录一起清。
    status_file = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
    data = _read_status_file(status_file) if status_file.exists() else None

    deleted_files: List[str] = []
    for suffix in _TASK_ARTIFACT_SUFFIXES:
        path = NOTE_OUTPUT_DIR / f"{task_id}{suffix}"
        if path.exists():
            try:
                path.unlink()
                deleted_files.append(path.name)
            except OSError as e:
                logger.warning(f"删除任务文件失败 {path}: {e}")

    # 向量索引：留着会让「AI 问答」继续检索到已删除的笔记
    try:
        from app.services.vector_store import VectorStoreManager
        VectorStoreManager().delete_index(task_id)
    except Exception as e:
        logger.warning(f"删除向量索引失败 (task_id={task_id}): {e}")

    # 数据库记录：只有该视频再没有任何任务文件时才清（否则会连带其他笔记的归属）
    if data:
        video_id, platform = data.get("video_id"), data.get("platform")
        if video_id and not find_task_ids_by_video(video_id, platform):
            try:
                delete_task_by_video(video_id=video_id, platform=platform or "bilibili")
                logger.info(f"已清理数据库任务记录 (video_id={video_id})")
            except Exception as e:
                logger.warning(f"清理数据库任务记录失败 (video_id={video_id}): {e}")

    logger.info(f"任务已删除 (task_id={task_id})，清理文件 {len(deleted_files)} 个")
    return {"deleted": len(deleted_files), "files": deleted_files}


def reap_interrupted_tasks(process_started_at: float) -> int:
    """把上一个进程遗留的「非终态」任务标记为失败。

    后端崩溃 / 蓝屏 / 强杀会留下永远停在 PENDING、SUMMARIZING 的状态文件，
    前端就永远显示「排队中、第 N 位」——本进程既没有它的队列记录，也不可能
    再把它跑完。启动时统一收敛成 FAILED 并写明原因，比留个假排队诚实。
    只收敛「本进程启动前就存在」且「超过 30 秒没被写过」的文件，避免误伤
    刚提交/正在执行的任务。
    """
    if not NOTE_OUTPUT_DIR.exists():
        return 0
    reaped = 0
    cutoff = process_started_at - 30
    for f in NOTE_OUTPUT_DIR.glob("*.status.json"):
        stem = f.name[: -len(".status.json")]
        if "_" in stem:
            continue
        try:
            if f.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        data = _read_status_file(f)
        if not data:
            continue
        status = data.get("status")
        if not status or status in TERMINAL_STATUSES:
            continue
        if is_task_active(stem):
            continue
        data["status"] = TaskStatus.FAILED.value
        data["message"] = "后端在任务执行中重启（或异常退出），任务已中断；请重新生成这份笔记"
        try:
            tmp = f.with_suffix(".status.json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(f)
            reaped += 1
            logger.warning(f"收敛中断任务：{stem}（原状态 {status}）→ FAILED")
        except OSError as e:
            logger.warning(f"写入状态文件失败 {f}: {e}")
    return reaped

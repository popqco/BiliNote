from abc import ABC
import os

from openai import PermissionDeniedError

from app.decorators.timeit import timeit
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.provider import ProviderService
from app.transcriber.base import Transcriber
from app.utils.logger import get_logger
from app.utils.openai_client import build_openai_client
import ffmpeg
import tempfile
from dotenv import load_dotenv
load_dotenv()

logger = get_logger(__name__)
MAX_SIZE_MB = 18
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024
# 自适应压缩的码率下限（16k 单声道对 whisper 仍可用）；约 3 小时以上的音频
# 压到下限仍会超限，此时明确报错而不是让上传被服务端掐断
_MIN_BITRATE_KBPS = 16

def compress_audio(input_path: str, target_bitrate='64k') -> str:
    output_fd, output_path = tempfile.mkstemp(suffix=".mp3")  # 临时输出文件
    os.close(output_fd)  # ffmpeg 会用路径操作
    ffmpeg.input(input_path).output(output_path, audio_bitrate=target_bitrate, ac=1).run(quiet=True, overwrite_output=True)
    return output_path

def fit_size_audio(input_path: str) -> str:
    """把超过上传上限的音频按时长自适应码率压到上限以内（单声道）。

    根因（2026-10-06「Skill 工作流对话」65.6 分钟视频实锤）：下载管线产出的
    就是 64k mp3（65 分钟 ≈ 30MB），之前超限后仍按固定 64k 重压缩——大小
    不变，超出 Groq 25MB 上限的上传被服务端中途掐断，openai SDK 报
    `Connection error.`，且每次重试都在 %TEMP% 泄漏一份 30MB 临时文件。
    """
    duration = float(ffmpeg.probe(input_path)["format"]["duration"])
    # -8k：容器/帧头开销余量，保证产物落在上限以内
    budget_kbps = max(_MIN_BITRATE_KBPS, int(MAX_SIZE_BYTES * 8 / duration / 1000) - 8)
    output_path = compress_audio(input_path, target_bitrate=f"{budget_kbps}k")
    size = os.path.getsize(output_path)
    if size > MAX_SIZE_BYTES:
        os.unlink(output_path)
        raise Exception(
            f"音频时长约 {duration / 60:.0f} 分钟，压到最低码率后仍有 {size / 1048576:.1f}MB，"
            f"超出转写引擎 {MAX_SIZE_MB}MB 上限；请改用本地转写引擎或先分段生成"
        )
    return output_path

class GroqTranscriber(Transcriber, ABC):


    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        file_size = os.path.getsize(file_path)
        compressed_path = None
        if file_size > MAX_SIZE_BYTES:
            print(f"文件超过 {MAX_SIZE_MB}MB，开始自适应压缩（当前 {round(file_size / (1024 * 1024), 2)}MB）...")
            file_path = fit_size_audio(file_path)
            compressed_path = file_path
            print(f"压缩完成，临时路径：{file_path}（{round(os.path.getsize(file_path) / (1024 * 1024), 2)}MB）")
        try:
            provider = ProviderService.get_provider_by_id('groq')

            if not provider:
                raise Exception("Groq 供应商未配置,请配置以后使用。")
            # build_openai_client 会校验 api_key 非空（空 key 会抛天书般的
            # `Illegal header value b'Bearer '`），并自动注入全局代理
            client = build_openai_client(
                api_key=provider.get('api_key'),
                base_url=provider.get('base_url'),
                key_label="Groq 转写引擎的 API Key",
            )
            filename = file_path
            model = os.getenv('GROQ_TRANSCRIBER_MODEL') or 'whisper-large-v3-turbo'

            def _create(cli):
                # GROQ_TRANSCRIBER_MODEL 未配置时 os.getenv 返回 None，Groq 会报
                # 400 invalid_model（'`model` is a required property'）——任务直接失败。
                # 这里给缺省 whisper-large-v3-turbo（与 .env 注释一致），避免静默 None。
                # 每次调用都重新打开文件：file.read() 会把句柄读空，降级重试需要新句柄。
                with open(filename, "rb") as file:
                    return cli.audio.transcriptions.create(
                        file=(filename, file.read()),
                        model=model,
                        response_format="verbose_json",
                    )

            try:
                transcription = _create(client)
            except PermissionDeniedError as exc:
                # 2026-10-08 实锤：该中转站对国内直连 IP 一律 403
                # （同一 key 走系统代理即 200），而应用客户端默认
                # trust_env=False 绕系统代理（防 Clash 对流式 LLM 请求假死）。
                # 转写是一次性大请求、走代理没有流式假死问题——403 时用
                # 系统代理重建客户端重试一次；仍失败则按普通错误上抛。
                logger.warning(f"转写直连被拒（{exc}），改用系统代理重试一次")
                proxy_client = build_openai_client(
                    api_key=provider.get('api_key'),
                    base_url=provider.get('base_url'),
                    key_label="Groq 转写引擎的 API Key",
                    use_system_proxy=True,
                )
                transcription = _create(proxy_client)
            print(transcription.text)
            segments = []
            full_text = ""

            for seg in transcription.segments:
                text = seg.text.strip()
                full_text += text + " "
                segments.append(TranscriptSegment(
                    start=seg.start,
                    end=seg.end,
                    text=text
                ))

            result = TranscriptResult(
                language=transcription.language,
                full_text=full_text.strip(),
                segments=segments,
                raw=transcription.to_dict()
            )
            return result
        finally:
            # 压缩产物是一次性临时文件（mkstemp），之前每次重试都在 %TEMP%
            # 泄漏一份 30MB（2026-10-06 实测一次排查清出 12 份）
            if compressed_path:
                try:
                    os.unlink(compressed_path)
                except OSError:
                    pass

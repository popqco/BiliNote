from dataclasses import dataclass
from typing import Optional

from app.models.audio_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult


@dataclass
class NoteResult:
    markdown: str                  # GPT 总结的 Markdown 内容
    transcript: TranscriptResult                # Whisper 转写结果
    audio_meta: AudioDownloadResult  # 音频下载的元信息（title、duration、封面等）
    # 生成参数快照：徽标（模型/风格）的数据源。后端此前从未持久化 model_name/style，
    # 前端只能靠本地提交时的 formData——自动化任务与 Viewer 回填的笔记徽标永远空白。
    # 全部 Optional + 默认值：老结果文件没有这些键，json.load 不走 dataclass 构造，
    # 不影响读取；新笔记写入时带上。
    model_name: Optional[str] = None
    provider_id: Optional[str] = None
    style: Optional[str] = None
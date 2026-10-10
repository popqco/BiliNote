"""超长音频的分段转写编排器。

背景（2026-10-10 实锤）：262 分钟的音频压到下限码率仍有 60.1MB，超出远端转写
引擎上传上限——且 mp3 在 44.1kHz 下的编码器下限是 32k（16k 请求被静默钳高），
压缩这条路对超长音频物理上走不通，只能分段。

设计（用户已拍板「等长硬切」）：
- 触发判定在调用方（note.py）：引擎声明 max_audio_size_bytes、原始文件超限、
  且按下限码率（16kbps → 2000 B/s）估的压缩结果仍会超限；只超大小但压得进的
  音频走引擎自带 fit_size_audio 即可，不切分。
- 切分：ffmpeg -ss/-t -c copy 从原始音频等长硬切，瞬时完成且零转码损失；
  切点可能落在词中间，极偶尔丢/糊 1-2 个词（约每 2 小时一个切点），对总结无感。
- 逐段转写：直接调 transcriber.transcript(part_path)，段超限时引擎内部
  fit_size_audio 按段时长自适应压缩（compress_audio 已带 ar=16000）。
- 断点续跑：段级缓存 {task_id}_transcript_part{i}.json 写在 note_results，
  任务失败时保留、重试时跳过已成功段；调用方在整体合并写盘成功后用
  cleanup_part_caches 清理；start 时顺手清扫 7 天前的陈旧段缓存。
- 合并：段 i 的 segments 按名义起点 i*part_dur 平移（与 TranscriptSegment
  「相对音频开头秒数」语义一致，下游 prompt/跳转链接/向量索引零改动）；
  language 取第一段，full_text 拼接；raw 置 None（当前无任何消费点）。
"""
import json
import math
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import ffmpeg

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 下限码率对应的字节速率：16kbps ÷ 8 = 2000 B/s（与 groq._MIN_BITRATE_KBPS 同源，
# 16kHz 采样率下该码率合法，不再被编码器钳到 32k）
FLOOR_BYTES_PER_SEC = 2000
# 分段预算安全系数：按 95% 上限估段长，吸收容器开销
SAFETY_FACTOR = 0.95
_PART_RETRIES = 3
_PART_RETRY_DELAYS = (3.0, 6.0)
STALE_PART_CACHE_DAYS = 7


def probe_duration(path: str) -> float:
    """ffprobe 音频时长（秒）；失败原样抛出，调用方自行兜底。"""
    return float(ffmpeg.probe(path)["format"]["duration"])


def plan_segment_count(duration: float, max_bytes: int) -> int:
    """按下限码率预估的分段数；触发条件保证 ratio>1，故至少 2 段。"""
    est_bytes = duration * FLOOR_BYTES_PER_SEC
    return max(2, math.ceil(est_bytes / (max_bytes * SAFETY_FACTOR)))


def segment_audio(input_path: str, n: int, work_dir: Path) -> List[Tuple[str, float]]:
    """等长硬切成 n 段，返回 [(分片路径, 名义起点秒)]。

    -ss 放在 -i 前是输入级快速 seek，配合 -c copy 落在最近的帧边界（mp3 一帧
    约 26ms），时间戳漂移可忽略；末段不带 -t 直接到 EOF。
    """
    duration = probe_duration(input_path)
    part_dur = duration / n
    ext = Path(input_path).suffix or ".mp3"
    parts: List[Tuple[str, float]] = []
    for i in range(n):
        start = i * part_dur
        out_path = str(work_dir / f"segment_{i}{ext}")
        out_kwargs = {"c": "copy"}
        if i < n - 1:
            out_kwargs["t"] = part_dur
        (
            ffmpeg.input(input_path, ss=start)
            .output(out_path, **out_kwargs)
            .run(quiet=True, overwrite_output=True)
        )
        parts.append((out_path, start))
    return parts


def merge_results(part_starts: List[float], results: Dict[int, TranscriptResult]) -> TranscriptResult:
    """按段的名义起点平移合并；language 取第一个非空段，raw 置 None。"""
    all_segments: List[TranscriptSegment] = []
    texts: List[str] = []
    language: Optional[str] = None
    for i in sorted(results):
        res = results[i]
        offset = part_starts[i]
        for seg in res.segments:
            all_segments.append(TranscriptSegment(start=seg.start + offset, end=seg.end + offset, text=seg.text))
        if res.full_text:
            texts.append(res.full_text.strip())
        if language is None and res.language:
            language = res.language
    return TranscriptResult(language=language, full_text=" ".join(texts), segments=all_segments, raw=None)


def cleanup_part_caches(part_cache_dir, task_id: Optional[str]) -> None:
    """整体合并成功后删掉该任务的段缓存文件。"""
    if not (part_cache_dir and task_id):
        return
    for f in Path(part_cache_dir).glob(f"{task_id}_transcript_part*.json"):
        try:
            f.unlink()
        except OSError:
            pass


def _sweep_stale_part_caches(part_cache_dir, max_age_days: int = STALE_PART_CACHE_DAYS) -> None:
    """清扫 7 天前的陈旧段缓存：自动化每轮重提会换新 task_id，旧任务的段缓存
    留下来只会膨胀磁盘；超龄的一律作废（丢的只是断点续跑机会）。"""
    if not part_cache_dir:
        return
    cutoff = time.time() - max_age_days * 86400
    try:
        candidates = list(Path(part_cache_dir).glob("*_transcript_part*.json"))
    except OSError:
        return
    for f in candidates:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _part_cache_file(part_cache_dir, task_id: Optional[str], index: int) -> Optional[Path]:
    if not (part_cache_dir and task_id):
        return None
    return Path(part_cache_dir) / f"{task_id}_transcript_part{index}.json"


def _load_part_cache(cache_file: Optional[Path]) -> Optional[TranscriptResult]:
    if not cache_file or not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        segments = [
            TranscriptSegment(start=float(s["start"]), end=float(s["end"]), text=str(s.get("text") or ""))
            for s in data.get("segments", [])
        ]
        return TranscriptResult(language=data.get("language"), full_text=data.get("full_text") or "", segments=segments)
    except Exception as e:
        # 写盘中断的半截文件按无缓存处理，丢这段缓存重转即可
        logger.warning(f"分段转写缓存读取失败，该段将重新转写：{e}")
        return None


def _write_part_cache(cache_file: Optional[Path], result: TranscriptResult) -> None:
    if not cache_file:
        return
    try:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(result), ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_file)
    except Exception as e:
        logger.warning(f"写入分段转写缓存失败（不影响转写本体）：{e}")


def _transcribe_part_with_retry(
    transcriber, part_path: str, index: int, n: int
) -> TranscriptResult:
    """与 note.py 单文件路径同款退避节奏（3 次尝试，3s/6s）。"""
    for attempt in range(1, _PART_RETRIES + 1):
        try:
            return transcriber.transcript(file_path=part_path)
        except Exception as exc:
            if attempt >= _PART_RETRIES:
                raise RuntimeError(f"第 {index + 1}/{n} 段转写失败（已重试 {_PART_RETRIES} 次）：{exc}") from exc
            delay = _PART_RETRY_DELAYS[attempt - 1]
            logger.warning(f"第 {index + 1}/{n} 段转写失败（第 {attempt} 次），{delay:.0f}s 后重试：{exc}")
            time.sleep(delay)


def transcribe_segmented(
    transcriber,
    audio_path: str,
    max_bytes: int,
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
    part_cache_dir=None,
    task_id: Optional[str] = None,
) -> TranscriptResult:
    """把超限音频切 n 段逐段转写后合并；异常上抛，已成功段的缓存留给断点续跑。

    :param on_progress: 每段开始真实转写（缓存命中跳过段不计）前回调 (第几段, 总段数)
    """
    duration = probe_duration(audio_path)
    n = plan_segment_count(duration, max_bytes)
    part_starts = [i * (duration / n) for i in range(n)]
    logger.info(
        f"音频时长 {duration / 60:.0f} 分钟，超出转写引擎 {max_bytes / 1048576:.0f}MB 上限，"
        f"启用分段转写：共 {n} 段（每段约 {duration / n / 60:.0f} 分钟）"
    )
    if part_cache_dir:
        _sweep_stale_part_caches(part_cache_dir)
    work_dir = Path(tempfile.mkdtemp(prefix="bilinote_segments_"))
    try:
        parts = segment_audio(audio_path, n, work_dir)
        results: Dict[int, TranscriptResult] = {}
        for i, (part_path, part_start) in enumerate(parts):
            cache_file = _part_cache_file(part_cache_dir, task_id, i)
            cached = _load_part_cache(cache_file)
            if cached is not None:
                logger.info(f"段 {i + 1}/{n} 命中断点缓存，跳过")
                results[i] = cached
                continue
            if on_progress:
                try:
                    on_progress(i + 1, n)
                except Exception:
                    pass
            logger.info(f"开始转写第 {i + 1}/{n} 段（自 {part_start:.0f}s 起）")
            results[i] = _transcribe_part_with_retry(transcriber, part_path, i, n)
            _write_part_cache(cache_file, results[i])
        merged = merge_results(part_starts, results)
        logger.info(f"分段转写合并完成：{len(parts)} 段 → {len(merged.segments)} 个片段")
        return merged
    finally:
        # 分片是临时产物；段缓存 JSON 在 note_results，由调用方整体成功后清理
        shutil.rmtree(work_dir, ignore_errors=True)

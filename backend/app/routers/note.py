# app/routers/note.py
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel, field_validator, model_validator

from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TERMINAL_STATUSES, TaskStatus
from app.db.video_task_dao import upsert_task_video_id
from app.exceptions.note import NoteError
from app.paths import NOTE_OUTPUT_DIR, UPLOAD_DIR
from app.services import task_state
from app.services.note import logger
from app.services.note_tasks import NoteTaskError, note_task_service
from app.services.proxy_config_manager import ProxyConfigManager, proxy_reachable
from app.services.task_serial_executor import task_serial_executor
from app.services.video_meta import fetch_video_meta
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import normalize_video_url
from app.validators.video_url_validator import is_supported_video_url
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response
import asyncio
import hashlib
import httpx
from PIL import Image, ImageOps

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()

# /tasks/recent 的进程内缓存：key = (状态目录, limit, 指纹)。
# 解析整目录实测 ~130ms；手机每 30s 轮询一次，指纹没变时直接复用结果，
# 并发请求也不会各自把目录再解析一遍（缓存条目只在指纹变化时新增）。
_recent_cache: dict = {}
_recent_cache_lock = threading.Lock()


class RecordRequest(BaseModel):
    # 删除任务的入参：至少给 task_id 或 video_id 之一。
    # 旧前端只传 {video_id, platform}，且 platform 可能是 undefined（整个键被
    # JSON.stringify 丢掉）——历史上这里会直接 422，前端只看到「服务器错误，
    # 请稍后再试」+「删除任务失败」两条红条（2026-10-01 实测复现）。全部改成
    # 可选，缺什么由服务端自己补。
    video_id: Optional[str] = None
    platform: Optional[str] = None
    task_id: Optional[str] = None
    # 强行删除正在生成中的任务（默认不允许，避免删了又被下一次状态写入复活）
    force: Optional[bool] = False


class VideoRequest(BaseModel):
    video_url: str
    platform: str
    quality: DownloadQuality
    screenshot: Optional[bool] = False
    link: Optional[bool] = False
    model_name: str
    provider_id: str
    task_id: Optional[str] = None
    format: Optional[list] = []
    style: str = None
    extras: Optional[str]=None
    video_understanding: Optional[bool] = False
    video_interval: Optional[int] = 0
    grid_size: Optional[list] = []
    # 原片截图密度（2026-10-27）：sparse/normal/rich；不传走全局配置。
    screenshot_density: Optional[str] = None
    # 客户端（如浏览器插件）已经在用户浏览器里抓到字幕，直接传给后端复用，
    # 跳过 download_subtitles 和音频转写。形如：
    #   {"language": "zh", "full_text": "...", "segments": [{"start","end","text"}, ...]}
    prefetched_transcript: Optional[dict] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_url(cls, data):
        # 稍后再看/收藏夹/带追踪参数的 B 站链接先规范化成标准 /video/BVxxx 形式，
        # 后续校验和 yt-dlp 下载拿到的都是干净链接
        if isinstance(data, dict) and data.get("platform") == "bilibili" and data.get("video_url"):
            data["video_url"] = normalize_video_url(str(data["video_url"]))
        return data

    @field_validator("video_url")
    def validate_supported_url(cls, v):
        url = str(v)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            # 是网络链接，继续用原有平台校验
            if not is_supported_video_url(url):
                from app.services.watchlater import is_watchlater_list_url
                if is_watchlater_list_url(url):
                    # 稍后再看「列表页」链接：走批量导入（generate_note 内处理）
                    return v
                if "/list/" in url:
                    # 其它列表页链接（如收藏夹）暂不支持
                    raise NoteError(
                        code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                        message="暂不支持该列表页链接：目前支持「稍后再看」列表页，或单个视频链接",
                    )
                raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                                message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)

        return v


def run_note_task(task_id: str, video_url: str, platform: str, quality: DownloadQuality,
                  link: bool = False, screenshot: bool = False, model_name: str = None, provider_id: str = None,
                  _format: list = None, style: str = None, extras: str = None, video_understanding: bool = False,
                  video_interval=0, grid_size=[], video_id: str = None
                  ):
    """兼容入口：执行编排已移到 NoteTaskService。"""
    return note_task_service.run(
        task_id, video_url, platform, quality,
        link=link, screenshot=screenshot,
        model_name=model_name, provider_id=provider_id,
        _format=_format, style=style, extras=extras,
        video_understanding=video_understanding,
        video_interval=video_interval, grid_size=grid_size, video_id=video_id,
        output_dir=NOTE_OUTPUT_DIR,
    )


@router.post("/cancel_task")
def cancel_task(data: RecordRequest):
    """停止正在生成（或排队中）的任务。

    用途（2026-10-16 用户需求）：模型/参数设置错了时不必干等整条流程跑完
    （含转写重试、总结降级阶梯这类「一直不停地尝试」）——点停止即刻中止，
    改完设置可以直接重新生成。

    行为：给当前执行打取消标记（阶段边界/重试间隙/流式增量处立刻生效），
    状态立即落 CANCELLED，前端与自动化都把「已停止」当终态（不是失败）。
    """
    task_id = (data.task_id or "").strip()
    if not task_id:
        return R.error(msg="缺少 task_id，无法停止任务", code=400)

    # 磁盘状态：判断「没有 live run」时是已结束还是残留
    disk_status = (task_state.read_status(task_id, root=NOTE_OUTPUT_DIR) or {}).get("status")

    live = task_state.request_cancel_task(task_id)
    if live:
        # 立即落盘，前端不用等当前步骤退出就能看到「已停止」；
        # 正在跑的 run 收尾时会再写一次终态文案（其状态写入已被抑制，只允许 CANCELLED）。
        task_state.write_status(
            task_id, TaskStatus.CANCELLED,
            message="已停止生成（正在中止当前步骤…）",
            root=NOTE_OUTPUT_DIR,
        )
        logger.info(f"停止任务已受理：task_id={task_id}（live run）")
        return R.success({"cancelled": True, "task_id": task_id, "live": True})

    if disk_status in TERMINAL_STATUSES:
        # 已经结束的任务：清掉刚落的预取消墓碑，不误伤将来的重试
        task_state.clear_cancel_request(task_id)
        return R.success({
            "cancelled": False,
            "task_id": task_id,
            "message": "任务已结束，无需停止",
            "status": disk_status,
        })

    # 磁盘非终态但没有 live run：本进程没在跑它（进程重启残留 / 提交后线程还没起）。
    # request_cancel_task 已落墓碑，迟到的 run 会立即取消；这里顺手把状态收敛为
    # CANCELLED，前端不必等下一个轮询周期。
    task_state.write_status(task_id, TaskStatus.CANCELLED,
                            message="已停止生成（用户手动停止）", root=NOTE_OUTPUT_DIR)
    logger.info(f"停止任务：无 live run，已按终态收敛 task_id={task_id}（原状态 {disk_status}）")
    return R.success({"cancelled": True, "task_id": task_id, "live": False})


@router.post('/delete_task')
def delete_task(data: RecordRequest):
    """真正删除任务：状态文件 + 缓存 + 导出结果 + 向量索引。

    之前这里是 TODO 空实现（只回一句「删除成功」），前端把卡片从本地列表里抹掉，
    但后端的 {task_id}.status.json 还在 → 30 秒后 /tasks/recent 增量同步又把它
    同步回来，用户看到「删掉的视频过一会儿又出现在列表里/又变成排队中」。
    """
    task_id = (data.task_id or "").strip()
    video_id = (data.video_id or "").strip()

    if not task_id and not video_id:
        return R.error(msg="缺少 task_id 或 video_id，无法删除", code=400)

    task_ids = [task_id] if task_id else task_state.find_task_ids_by_video(
        video_id, data.platform, root=NOTE_OUTPUT_DIR
    )
    if not task_ids:
        # 已经是「不存在」的目标状态，按成功处理，避免前端把卡片又加回来
        return R.success({"deleted": 0, "task_ids": [], "message": "任务不存在或已被删除"})

    running = [t for t in task_ids if task_state.is_task_active(t)]
    if running and not data.force:
        return R.error(
            msg="该任务正在生成中，无法删除；请等它生成完成或失败后再删",
            code=400,
            data={"active": True, "task_ids": running},
        )

    total = 0
    for tid in task_ids:
        total += task_state.purge_task(tid, root=NOTE_OUTPUT_DIR).get("deleted", 0)

    logger.info(f"删除任务完成：task_ids={task_ids}, 清理文件 {total} 个")
    return R.success({"deleted": total, "task_ids": task_ids})


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_location = os.path.join(UPLOAD_DIR, file.filename)

    with open(file_location, "wb+") as f:
        f.write(await file.read())

    # 假设你静态目录挂载了 /uploads
    return R.success({"url": f"/uploads/{file.filename}"})


@router.post("/generate_note")
def generate_note(data: VideoRequest, background_tasks: BackgroundTasks):
    try:
        result = note_task_service.submit(
            video_url=str(data.video_url),
            platform=data.platform,
            quality=data.quality,
            link=data.link,
            screenshot=data.screenshot,
            model_name=data.model_name,
            provider_id=data.provider_id,
            formats=data.format,
            style=data.style,
            extras=data.extras,
            video_understanding=data.video_understanding,
            video_interval=data.video_interval,
            grid_size=data.grid_size,
            screenshot_density=data.screenshot_density,
            task_id=data.task_id,
            prefetched_transcript=data.prefetched_transcript,
            schedule=background_tasks.add_task,
            output_dir=NOTE_OUTPUT_DIR,
        )
        return R.success(result.as_response())
    except NoteTaskError as e:
        return R.error(msg=e.message, code=e.code, data=e.data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RefinalizeRequest(BaseModel):
    """存量笔记重整理入参（2026-10-14）：默认沿用结果文件记录的模型，可覆盖。"""
    task_id: str
    model_name: Optional[str] = None
    provider_id: Optional[str] = None
    format: Optional[list] = None


@router.post("/refinalize_note")
def refinalize_note(data: RefinalizeRequest):
    """对已生成的笔记重跑「合并后整理」：摘掉目录/总结残留 → 重建目录 → 重写 AI 总结。

    只读结果文件 + 一次纯文本模型调用，不重新下载/转写/喂图——用于把存量
    笔记修复到新版整理逻辑（合并阶段产出的「本段/本批」堆叠式总结），
    不必让用户重新生成整条笔记。
    """
    from app.services.note import refinalize_stored_note
    try:
        out = refinalize_stored_note(
            data.task_id,
            model_name=data.model_name,
            provider_id=data.provider_id,
            formats=data.format,
        )
    except FileNotFoundError as e:
        return R.error(msg=str(e), code=404)
    except Exception as e:
        logger.error(f"重整理笔记失败 (task_id={data.task_id}): {e}", exc_info=True)
        return R.error(msg=f"重整理失败：{e}", code=500)
    md = out["markdown"]
    return R.success({
        "task_id": out["task_id"],
        "formats": out["formats"],
        "model_name": out["model_name"],
        "length": len(md),
        "preview": md[:1500],
    })


@router.post("/task_refresh_meta/{task_id}")
def refresh_task_meta(task_id: str):
    """重新抓取并回填存量任务的标题/封面/时长/video_id。

    2026-10-24 的「未命名笔记」：提交时 DNS 瞬时失败，audio_meta 全 null 被
    写进 _audio.json，之后重试复用缓存，标题/封面永远缺失。这个接口只做一次
    轻量元信息请求并同步状态文件/结果文件/数据库，不重跑下载与转写。
    """
    status_path = NOTE_OUTPUT_DIR / f"{task_id}.status.json"
    if not status_path.exists():
        return R.error(msg="任务不存在", code=404)
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as e:
        return R.error(msg=f"读取任务状态失败：{e}", code=500)

    video_url = status_data.get("video_url")
    platform = status_data.get("platform") or "bilibili"
    if not video_url:
        return R.error(msg="任务缺少视频链接，无法刷新元信息", code=400)

    meta = fetch_video_meta(str(video_url), platform)
    if not meta or not meta.get("title"):
        return R.error(msg="未能获取到标题/封面（视频可能已删除、受限，或网络暂不可达），请稍后重试", code=502)

    meta_fields = {
        k: meta.get(k)
        for k in ("title", "cover_url", "duration", "video_id")
        if meta.get(k) not in (None, "")
    }
    if meta.get("platform"):
        meta_fields["platform"] = meta.get("platform")

    # 1) 状态文件（历史卡片/阅读页优先读这里）
    audio_meta = status_data.get("audio_meta")
    audio_meta = dict(audio_meta) if isinstance(audio_meta, dict) else {}
    audio_meta.update(meta_fields)
    audio_meta.setdefault("platform", platform)
    status_data["audio_meta"] = audio_meta
    if not status_data.get("video_id") and meta_fields.get("video_id"):
        status_data["video_id"] = meta_fields["video_id"]
    status_path.write_text(json.dumps(status_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 完整结果文件
    result_path = NOTE_OUTPUT_DIR / f"{task_id}.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result_audio = result.get("audio_meta")
            result_audio = dict(result_audio) if isinstance(result_audio, dict) else {}
            result_audio.update(meta_fields)
            result_audio.setdefault("platform", platform)
            result["audio_meta"] = result_audio
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"刷新元信息时更新结果文件失败 (task_id={task_id}): {e}")

    # 3) 音频缓存（重试命中缓存时会读取；不刷新会再次把空值带回来）
    audio_path = NOTE_OUTPUT_DIR / f"{task_id}_audio.json"
    if audio_path.exists():
        try:
            audio_data = json.loads(audio_path.read_text(encoding="utf-8"))
            if isinstance(audio_data, dict):
                audio_data.update(meta_fields)
                audio_data.setdefault("platform", platform)
                audio_path.write_text(json.dumps(audio_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"刷新元信息时更新音频缓存失败 (task_id={task_id}): {e}")

    # 4) 数据库任务记录（自动化去重依赖 video_id）
    final_video_id = status_data.get("video_id") or meta_fields.get("video_id")
    if final_video_id:
        upsert_task_video_id(task_id, str(final_video_id), platform)

    logger.info(f"已刷新任务元信息 (task_id={task_id}, title={meta_fields.get('title')})")
    return R.success({
        "task_id": task_id,
        "audio_meta": audio_meta,
        "video_id": final_video_id,
    })


_NOTE_RESULT_KEYS = (
    "markdown",
    "model_name",
    "provider_id",
    "style",
    "quality",
    "format",
    "link",
    "screenshot",
    "extras",
    "video_understanding",
    "video_interval",
    "grid_size",
    "video_url",
)
_LIGHT_AUDIO_KEYS = ("title", "cover_url", "duration", "platform", "video_id")


def _project_note_result(result: dict) -> dict:
    """笔记阅读视图：只留正文和轻量元信息。

    `task_status` 默认仍返回完整结果（MCP / 导出 / 旧客户端依赖 transcript、
    audio_meta.raw_info）。远端手机打开笔记时只要 markdown：实测整包里
    transcript 占 39%、raw_info 占 30%，这两块在阅读首屏完全用不到，却要
    多传十几到几百 KB。`view=note` 就返回这个投影。
    """
    if not isinstance(result, dict):
        return result
    out = {k: result.get(k) for k in _NOTE_RESULT_KEYS if k in result}
    audio = result.get("audio_meta") or {}
    if isinstance(audio, dict):
        light = {k: audio.get(k) for k in _LIGHT_AUDIO_KEYS if audio.get(k) not in (None, "")}
        raw = audio.get("raw_info") or {}
        if isinstance(raw, dict):
            # VideoBanner / 导出海报要的上传者与原视频地址，从 51 键 raw_info
            # 里只抽这两个，别把整包再带回去。
            for k in ("uploader", "webpage_url"):
                if raw.get(k):
                    light[k] = raw[k]
        if light:
            out["audio_meta"] = light
    return out


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str, view: str = "full"):
    note_view = str(view or "").lower() == "note"
    # 优先读状态文件
    status_content = task_state.read_status(task_id, root=NOTE_OUTPUT_DIR)
    if status_content is not None:
        status = status_content.get("status")
        message = status_content.get("message", "")
        audio_meta = status_content.get("audio_meta")

        if status == TaskStatus.SUCCESS.value:
            # 成功状态的话，继续读取最终笔记内容
            result_content = task_state.read_result(task_id, root=NOTE_OUTPUT_DIR)
            if result_content is not None:
                if note_view:
                    result_content = _project_note_result(result_content)
                return R.success({
                    "status": status,
                    "result": result_content,
                    "message": message,
                    "task_id": task_id
                })
            else:
                # 理论上不会出现，保险处理
                return R.success({
                    "status": TaskStatus.PENDING.value,
                    "message": "任务完成，但结果文件未找到",
                    "task_id": task_id
                })

        if status == TaskStatus.FAILED.value:            # P1-③：FAILED-but-has-result 允许查看。重试/同视频复用 task_id 时，
            # 老结果文件（{task_id}.json）还在——直接 500 会让之前成功的内容也
            # 看不了。信封仍走 R.error（code=500，调用方按失败处理），只是把
            # 结果附在 data.result 里，前端失败卡片可展示。
            result_content = task_state.read_result(task_id, root=NOTE_OUTPUT_DIR)
            if result_content is not None:
                if note_view:
                    result_content = _project_note_result(result_content)
                return R.error(message or "任务失败", code=500, data={
                    "task_id": task_id,
                    "status": status,
                    "has_result": True,
                    "result": result_content,
                })
            return R.error(message or "任务失败", code=500)

        if status == TaskStatus.CANCELLED.value:
            # 已停止（用户主动）：成功信封 + 终态状态。前端按「已停止」展示
            # 并可一键重试；自动化不计失败（不算冷却、不发失败告警）。
            resp = {
                "status": status,
                "message": message or "已停止生成",
                "task_id": task_id,
            }
            if audio_meta:
                resp["audio_meta"] = audio_meta
            return R.success(resp)

        # 处理中状态：附带早期元信息（标题/封面）与排队位次
        resp = {
            "status": status,
            "message": message,
            "task_id": task_id,
        }
        if audio_meta:
            resp["audio_meta"] = audio_meta
        if status == TaskStatus.PENDING.value:
            pos = task_serial_executor.queue_position(task_id)
            if pos:
                resp["queue_position"] = pos
        return R.success(resp)

    # 没有状态文件，但有结果
    result_content = task_state.read_result(task_id, root=NOTE_OUTPUT_DIR)
    if result_content is not None:
        if note_view:
            result_content = _project_note_result(result_content)
        return R.success({
            "status": TaskStatus.SUCCESS.value,
            "result": result_content,
            "task_id": task_id
        })

    # 什么都没有，默认PENDING
    return R.success({
        "status": TaskStatus.PENDING.value,
        "message": "任务排队中",
        "task_id": task_id
    })


@router.get("/task_versions/{task_id}")
def get_task_versions(task_id: str):
    """同一视频的历史版本（归档旧笔记 + 当前结果 + 同视频旧任务）。

    生成中也可以调用：当前结果文件还是上一版，因此新版本跑完前用户就能
    先看历史版本；跑完后新结果会出现在列表首位。
    """
    from app.services.note_versions import list_task_versions
    try:
        versions = list_task_versions(task_id, NOTE_OUTPUT_DIR)
    except Exception as e:
        logger.warning(f"读取笔记版本失败 (task_id={task_id}): {e}")
        versions = []
    return R.success({"versions": versions})


@router.get("/task_transcript/{task_id}")
def get_task_transcript(task_id: str):
    """按需取字幕：笔记阅读首屏不带 transcript，打开「原文」面板再拉这一份。

    transcript.raw 是转写器原始响应（Groq 会重复一遍全文+全部分段），前端
    从不用它，这里直接剔除，只返回 full_text/language/segments。
    """
    result = task_state.read_result(task_id, root=NOTE_OUTPUT_DIR)
    if result is None:
        return R.error("笔记结果不存在", code=404)
    transcript = result.get("transcript")
    if not isinstance(transcript, dict):
        transcript = {"full_text": "", "language": "", "segments": []}
    transcript = {
        k: transcript.get(k)
        for k in ("full_text", "language", "segments")
        if transcript.get(k) is not None
    }
    return R.success({"transcript": transcript})


@router.get("/video_meta")
def get_video_meta(url: str, platform: str = "bilibili"):
    """快速返回视频标题/封面/时长（不下载），供前端在任务排队期间就展示卡片。

    失败也返回 success（title 为 null），前端静默忽略即可——元信息拿不到
    不能影响任何主流程。
    """
    try:
        meta = fetch_video_meta(url, platform)
    except Exception as e:
        logger.warning(f"video_meta 查询失败: {e}")
        meta = None
    return R.success(meta or {"title": None})


@router.get("/tasks/recent")
def get_recent_tasks(limit: int = 80, since: Optional[str] = None):
    """后端最近任务列表（扫描状态文件），供前端历史增量同步与自动化任务可见性。

    `since` 是上一次响应的 `version`：指纹未变时只回 `unchanged`，30s 轮询
    从约 45KB gzip 降到几百字节，也免掉整目录重新解析。
    """
    try:
        limit = max(1, min(int(limit), 300))
    except Exception:
        limit = 80
    version = task_state.recent_fingerprint(root=NOTE_OUTPUT_DIR)
    if since and since == version:
        return R.success({"tasks": [], "unchanged": True, "version": version})

    key = (str(NOTE_OUTPUT_DIR), limit, version)
    with _recent_cache_lock:
        cached = _recent_cache.get(key)
    if cached is None:
        cached = task_state.list_recent_tasks(limit=limit, root=NOTE_OUTPUT_DIR)
        with _recent_cache_lock:
            # 只留最近 16 个指纹快照，避免长期运行时随状态写入无限堆积
            if len(_recent_cache) >= 16:
                _recent_cache.clear()
            _recent_cache[key] = cached
    return R.success({"tasks": cached, "version": version})


# ---------------------------------------------------------------------------
# 封面图代理（/image_proxy）
#
# 2026-10-17 加固（用户反馈「老笔记封面有时候刷新不出来」），三个实测根因：
#  1. 上游 CDN（i*.hdslb.com / i.ytimg.com）单张 3~4s，没有缓存时历史列表
#     上百张封面要几分钟才逐个刷出来；
#  2. http://i.ytimg.com/... 之类 http 地址在 CDN 侧直接挂死（实测 15s 无响应），
#     只按原样请求一次必然失败——需要 https 变体兜底；
#  3. 旧实现不跟随重定向（302 直接当失败）、任何异常返回 500 且不重试，
#     一次网络抖动这张封面就废了。
#
# 现在：磁盘缓存（static/cover_cache，键=URL 的 sha1）→ 命中直接返回；
# 未命中按候选地址（原地址 + https 变体）各试两次（20s 超时 + 跟随重定向），
# 全失败返回 404 + no-store（前端 onError 回退占位图，也不会被浏览器缓存住）。
_COVER_CACHE_DIR = Path(os.getcwd()) / "static" / "cover_cache"
_COVER_CACHE_MAX_FILES = 1500
_cover_cache_prune_tick = 0
# 取不到的封面做短时负缓存（URL → 失败时间戳）：视频被删/封面被改的地址
# 每次浏览都去上游试一遍（每轮 3~12s）纯属浪费，10 分钟内直接按失败返回。
_COVER_FAIL_CACHE: dict[str, float] = {}
_COVER_FAIL_TTL_SECONDS = 600

_COVER_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}

# 图片缩放（封面缩略/笔记截图缩略）共用的独立线程池：PIL 解码 4K JPEG
# 是 CPU 密集操作，丢进 Starlette 默认线程池会和 /sys_check、状态读取抢
# 线程；固定 4 个 worker 既能把并发压住，又不阻塞事件循环。
_IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="img-resize")
_COVER_THUMB_MAX_WIDTH = 1600


def _resize_cover_bytes(content: bytes, width: int) -> Optional[bytes]:
    """把封面缩到指定宽度；原图本来更小或解码失败时返回 None（调用方回退原图）。"""
    try:
        with Image.open(io.BytesIO(content)) as im:
            im = ImageOps.exif_transpose(im) or im
            if im.width <= width:
                return None
            ratio = width / float(im.width)
            out = im.convert("RGB").resize(
                (width, max(1, round(im.height * ratio))), Image.LANCZOS
            )
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"封面缩放失败，回退原图（w={width}）: {e}")
        return None


def _normalize_image_url(url: str) -> str:
    """协议相对地址（//i.ytimg.com/...）补 https，否则 httpx 直接报 InvalidURL。"""
    u = (url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    return u


def _cached_cover_path(key: str) -> Optional[Path]:
    try:
        for p in _COVER_CACHE_DIR.glob(f"{key}.*"):
            if p.is_file() and p.stat().st_size > 0:
                return p
    except OSError:
        return None
    return None


def _maybe_prune_cover_cache() -> None:
    """文件数超过上限时按 mtime 从旧到新清理，避免缓存无限膨胀。"""
    global _cover_cache_prune_tick
    _cover_cache_prune_tick += 1
    if _cover_cache_prune_tick % 50 != 0:
        return
    try:
        files = [p for p in _COVER_CACHE_DIR.glob("*") if p.is_file()]
        if len(files) <= _COVER_CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - _COVER_CACHE_MAX_FILES]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _store_cached_cover(key: str, content_type: str, content: bytes) -> Optional[Path]:
    ext = _COVER_EXT_BY_TYPE.get(content_type, ".jpg")
    try:
        _COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _COVER_CACHE_DIR / f"{key}{ext}"
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
        _maybe_prune_cover_cache()
        return p
    except OSError:
        return None


def _cover_proxy_url(target: str) -> Optional[str]:
    """YouTube 封面使用应用里配置的代理；只对 ytimg/youtube 生效。

    2026-10-21 实测：本机直连 i.ytimg.com 20s 超时，经配置的 7892 代理
    0.8s 返回。不能只依赖进程启动时注入的环境变量——后端常驻运行时用户
    可能才打开代理，环境变量不会再更新。代理探活失败则退回直连。
    """
    if not ("ytimg" in target or "youtube" in target):
        return None
    url = ProxyConfigManager().get_proxy_url()
    if url and not proxy_reachable(url):
        logger.warning(f"封面代理：YouTube 代理不可达，退回直连：{url}")
        return None
    return url


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str, w: int = 0):
    target = _normalize_image_url(url)
    if not target.startswith(("http://", "https://")):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "无效的图片地址"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        width = int(w)
    except (TypeError, ValueError):
        width = 0
    width = max(0, min(width, _COVER_THUMB_MAX_WIDTH))

    # 宽度参与缓存键：历史卡 320 / 详情 800 / 海报 600 各存一份，互不覆盖。
    cache_key = target if width <= 0 else f"{target}|w={width}"
    key = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    cached = _cached_cover_path(key)
    if cached is not None:
        ctype = next(
            (ct for ct, ext in _COVER_EXT_BY_TYPE.items() if ext == cached.suffix),
            "image/jpeg",
        )
        return FileResponse(
            str(cached),
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # 负缓存命中的地址不再打上游（明细见 _COVER_FAIL_CACHE 定义处）
    failed_at = _COVER_FAIL_CACHE.get(key)
    if failed_at is not None:
        if time.time() - failed_at < _COVER_FAIL_TTL_SECONDS:
            return JSONResponse(
                status_code=404,
                content={"code": 404, "msg": "图片暂不可用（近期获取失败，稍后自动重试）"},
                headers={"Cache-Control": "no-store"},
            )
        _COVER_FAIL_CACHE.pop(key, None)

    # http 地址追加一个 https 变体：i.ytimg.com 等 CDN 只认 https，http 会挂死
    candidates = [target]
    if target.startswith("http://"):
        candidates.append("https://" + target[len("http://"):])

    is_youtube = "ytimg" in target or "youtube" in target
    # 显式读取应用代理配置（而非只靠启动时注入的环境变量）；探活在线程里做，
    # 避免最多 0.5s 的 TCP 连接阻塞事件循环。trust_env=False 同时避免继承
    # 到已失效的系统代理。
    proxy_url = await asyncio.to_thread(_cover_proxy_url, target)
    headers = {
        "Referer": "https://www.youtube.com/" if is_youtube else "https://www.bilibili.com/",
        "User-Agent": request.headers.get("User-Agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    last_err = ""
    for cand in candidates:
        for _ in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=20.0,
                    follow_redirects=True,
                    proxy=proxy_url,
                    trust_env=False,
                ) as client:
                    resp = await client.get(cand, headers=headers)
                if resp.status_code == 200 and resp.content:
                    ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
                    if not ctype.startswith("image/"):
                        ctype = "image/jpeg"
                    body = resp.content
                    if width > 0:
                        resized = await asyncio.get_running_loop().run_in_executor(
                            _IMAGE_EXECUTOR, _resize_cover_bytes, body, width
                        )
                        if resized:
                            body, ctype = resized, "image/jpeg"
                    p = _store_cached_cover(key, ctype, body)
                    if p is not None:
                        return FileResponse(
                            str(p),
                            media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"},
                        )
                    return Response(
                        content=body,
                        media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
                last_err = f"HTTP {resp.status_code}"
                # 明确的"没有/被拒"（403/404/410）：重试无意义，直接换候选地址
                if resp.status_code in (400, 403, 404, 410):
                    break
            except Exception as e:  # 网络级异常：换候选地址/重试
                last_err = str(e)
            await asyncio.sleep(0.4)

    logger.warning(f"封面代理失败: {target} ({last_err})")
    _COVER_FAIL_CACHE[key] = time.time()
    # 返回 404 而不是 500：图片"取不到"是常态（视频被删/封面被改），
    # 让前端走 onError 回退占位图；no-store 避免错误被缓存住
    return JSONResponse(
        status_code=404,
        content={"code": 404, "msg": f"图片获取失败: {last_err}"},
        headers={"Cache-Control": "no-store"},
    )

# ---------------------------------------------------------------------------
# 原片截图缩略（/img_thumb）
#
# 2026-10-17 白屏根因修复配套：笔记正文里的原片截图是 4K JPEG（3840×2160，
# 解码后单张约 31MB），重度笔记一篇引用可达数百张（实测最多 790 张）。
# WebView2 渲染进程按原始分辨率解码会把内存吃到 GB 级，被系统 OOM 杀掉后
# 页面永久白屏（crashpad 转储 ProcessType=renderer SubCode=0xe0000008，
# 同日 10:06/11:33/11:35/11:36 连崩四次）。屏幕显示统一走这里缩到 1600px
# 宽并落盘缓存——解码内存降一个数量级、传输体积降十倍；点击放大由前端
# 另行取原图（zoomImg），长图导出（html-to-image 自己按 src 拉图）也走缩略。
_SCREENSHOTS_DIR = Path(os.getcwd()) / "static" / "screenshots"
_THUMB_CACHE_DIR = Path(os.getcwd()) / "static" / "thumb_cache"
_THUMB_CACHE_MAX_FILES = 20000
_THUMB_DEFAULT_WIDTH = 1600
_thumb_cache_prune_tick = 0

_THUMB_MEDIA_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _resolve_local_screenshot(url: str) -> Optional[Path]:
    """笔记图片地址 → 本地截图文件，非本机截图返回 None。

    笔记里存的是相对路径（/static/screenshots/xxx.jpg），桌面端经 baseURL
    展开成 http://127.0.0.1:8483/...，远端手机可能是 http://<Worker-IP>:8483/...
    这些形态都只认 path 段、收敛到本地截图文件名；外链（没有该 path 的
    图床地址）不走缩略，调用方 404 让前端回退原图。host 不参与解析，因为
    最终只读 static/screenshots 下的直接文件名，任意 host 都越不出这个目录。
    """
    u = _normalize_image_url(url)
    parsed = urlparse(u)
    path = unquote(parsed.path or "")
    prefix = "/static/screenshots/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    # 只认直接文件名：任何目录段（../ 等 traversal 尝试）直接拒绝
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    f = _SCREENSHOTS_DIR / name
    try:
        if f.is_file() and f.stat().st_size > 0:
            return f
    except OSError:
        return None
    return None


def _thumb_cache_path(key: str) -> Optional[Path]:
    try:
        p = _THUMB_CACHE_DIR / f"{key}.jpg"
        if p.is_file() and p.stat().st_size > 0:
            return p
    except OSError:
        return None
    return None


def _maybe_prune_thumb_cache() -> None:
    """文件数超上限时按 mtime 从旧到新清理（缩略是可再生成的，无需精确）。"""
    global _thumb_cache_prune_tick
    _thumb_cache_prune_tick += 1
    if _thumb_cache_prune_tick % 100 != 0:
        return
    try:
        files = [p for p in _THUMB_CACHE_DIR.glob("*.jpg") if p.is_file()]
        if len(files) <= _THUMB_CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - _THUMB_CACHE_MAX_FILES]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _build_thumb(src: Path, width: int) -> Optional[Path]:
    """生成/复用缩略图，返回可直出文件的路径；失败返回 None（回退原图）。"""
    key = hashlib.sha1(f"{src.name}|w={width}".encode("utf-8")).hexdigest()
    cached = _thumb_cache_path(key)
    if cached is not None:
        return cached
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im) or im
            if im.width <= width:
                return src  # 原图本来就够小，不生成也不缓存
            ratio = width / float(im.width)
            out = im.convert("RGB").resize(
                (width, max(1, round(im.height * ratio))), Image.LANCZOS
            )
        _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _THUMB_CACHE_DIR / f"{key}.jpg"
        tmp = p.with_name(p.name + ".tmp")
        out.save(tmp, format="JPEG", quality=80, optimize=True)
        os.replace(tmp, p)
        _maybe_prune_thumb_cache()
        return p
    except Exception as e:  # 任何解码/写盘失败都回退原图，宁大勿断
        logger.warning(f"缩略图生成失败，回退原图: {src.name} ({e})")
        return None


@router.get("/img_thumb")
async def img_thumb(url: str, w: int = _THUMB_DEFAULT_WIDTH):
    src = _resolve_local_screenshot(url)
    if src is None:
        return JSONResponse(
            status_code=404,
            content={"code": 404, "msg": "不是本机原片截图，不做缩略"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        width = int(w)
    except (TypeError, ValueError):
        width = _THUMB_DEFAULT_WIDTH
    width = max(240, min(width, 4096))

    # PIL 解码/缩放放到独立线程池：不占用 Starlette 默认线程池，也不阻塞
    # 事件循环；并发上限 4，避免一屏几十张 4K 同时解码把 CPU 打满。
    path = await asyncio.get_running_loop().run_in_executor(
        _IMAGE_EXECUTOR, _build_thumb, src, width
    )
    path = path or src
    media = _THUMB_MEDIA_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(
        str(path),
        media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

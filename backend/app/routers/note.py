# app/routers/note.py
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel, validator, field_validator, model_validator
from dataclasses import asdict

from app.db.video_task_dao import get_task_by_video
from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.exceptions.note import NoteError
from app.services.note import (
    NoteGenerator,
    logger,
    active_task_for_video,
    find_active_task_by_video,
    list_recent_tasks,
    find_task_ids_by_video,
    is_task_active,
    mark_task_active,
    mark_task_done,
    purge_task,
)
from app.services.task_serial_executor import task_serial_executor, video_task_locks
from app.services.video_meta import fetch_video_meta
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import extract_video_id, normalize_video_url
from app.validators.video_url_validator import is_supported_video_url
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx
from app.enmus.task_status_enums import TaskStatus

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()


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


NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", "note_results")
UPLOAD_DIR = "uploads"


def save_note_to_file(task_id: str, note):
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(asdict(note), f, ensure_ascii=False, indent=2)


def _persist_prefetched_transcript(task_id: str, transcript: dict) -> None:
    """把客户端预取的字幕写到 NoteGenerator 期望的转写缓存文件里。

    NoteGenerator.generate 会优先读 <task_id>_transcript.json，命中即跳过 download_subtitles
    与音频转写流程。要求字段：language(可空)/full_text/segments[{start,end,text}]
    """
    segments = transcript.get("segments") or []
    cleaned_segments = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        cleaned_segments.append({
            "start": float(s.get("start", 0)),
            "end": float(s.get("end", 0)),
            "text": text,
        })
    if not cleaned_segments:
        raise ValueError("prefetched_transcript 没有可用的 segments")

    full_text = transcript.get("full_text") or " ".join(s["text"] for s in cleaned_segments)
    payload = {
        "language": transcript.get("language") or "zh",
        "full_text": full_text,
        "segments": cleaned_segments,
    }

    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    target = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}_transcript.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"已写入客户端预取字幕缓存: {target} ({len(cleaned_segments)} 段)")


def run_note_task(task_id: str, video_url: str, platform: str, quality: DownloadQuality,
                  link: bool = False, screenshot: bool = False, model_name: str = None, provider_id: str = None,
                  _format: list = None, style: str = None, extras: str = None, video_understanding: bool = False,
                  video_interval=0, grid_size=[], video_id: str = None
                  ):

    if not model_name or not provider_id:
        raise HTTPException(status_code=400, detail="请选择模型和提供者")

    def _execute_note_task():
        return NoteGenerator().generate(
            video_url=video_url,
            platform=platform,
            quality=quality,
            task_id=task_id,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            _format=_format,
            style=style,
            extras=extras,
            screenshot=screenshot,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
        )

    logger.info(f"任务进入执行队列 (task_id={task_id})")
    # 登记「本进程正在跑」：删除接口靠它拒绝删正在生成的任务，重复提交靠它
    # 拦住「同一个 task 被点两次重新生成」导致的状态回退。
    mark_task_active(task_id, video_id)
    try:
        # 同一视频互斥：防止同视频双任务并发下载/生成触发文件锁冲突（WinError 32）。
        # 不同视频互不阻塞，两个 worker 可让「转写（GPU）」与「总结（网络）」阶段重叠（ADR-0001）。
        with video_task_locks.get(video_id or task_id):
            note = task_serial_executor.run(_execute_note_task, task_id=task_id)
    finally:
        mark_task_done(task_id)
    logger.info(f"Note generated: {task_id}")
    if not note or not note.markdown:
        logger.warning(f"任务 {task_id} 执行失败，跳过保存")
        return
    save_note_to_file(task_id, note)

    # 自动建立向量索引（用于 AI 问答），失败不影响笔记生成
    try:
        from app.services.vector_store import VectorStoreManager
        VectorStoreManager().index_task(task_id)
    except Exception as e:
        logger.warning(f"向量索引失败（不影响笔记）: {e}")


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

    task_ids = [task_id] if task_id else find_task_ids_by_video(video_id, data.platform)
    if not task_ids:
        # 已经是「不存在」的目标状态，按成功处理，避免前端把卡片又加回来
        return R.success({"deleted": 0, "task_ids": [], "message": "任务不存在或已被删除"})

    running = [t for t in task_ids if is_task_active(t)]
    if running and not data.force:
        return R.error(
            msg="该任务正在生成中，无法删除；请等它生成完成或失败后再删",
            code=400,
            data={"active": True, "task_ids": running},
        )

    total = 0
    for tid in task_ids:
        total += purge_task(tid).get("deleted", 0)

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
        # 就绪门禁：本地转写引擎（fast-whisper / mlx-whisper）必须等模型下载完才能跑视频，
        # 否则任务会卡在首次下载（慢 / OOM / 截断），用户只看到一个静默失败的任务。
        # 客户端已抓好字幕（prefetched_transcript）则不需要转写，跳过检查。
        if not data.prefetched_transcript:
            from app.services.transcriber_config_manager import TranscriberConfigManager
            readiness = TranscriberConfigManager().is_model_ready()
            if not readiness["ready"]:
                logger.warning(f"拒绝 generate_note：{readiness['reason']}")
                return R.error(
                    msg=readiness["reason"],
                    code=300102,
                    data={
                        "reason": "transcriber_model_not_ready",
                        "transcriber_type": readiness["transcriber_type"],
                        "model_size": readiness["model_size"],
                        "downloading": readiness["downloading"],
                    },
                )

        # 稍后再看「列表页」链接：批量导入（拉列表 → 去重 → 逐条建任务）
        from app.services.watchlater import is_watchlater_list_url, fetch_watchlater
        if is_watchlater_list_url(str(data.video_url)):
            try:
                items = fetch_watchlater(max_items=50)
            except Exception as e:
                return R.error(msg=f"获取稍后再看列表失败：{e}", code=400)
            created = []
            for it in items:
                bv = it.get("bvid")
                if not bv:
                    continue
                if find_active_task_by_video(bv):
                    continue
                tid = str(uuid.uuid4())
                NoteGenerator()._update_status(
                    tid,
                    TaskStatus.PENDING,
                    extra={
                        "video_id": bv,
                        "platform": "bilibili",
                        "origin": "manual",
                        "video_url": it["video_url"],
                        "audio_meta": {
                            "title": it.get("title"),
                            "cover_url": it.get("cover_url"),
                            "video_id": bv,
                            "duration": it.get("duration"),
                            "platform": "bilibili",
                        },
                    },
                )
                background_tasks.add_task(
                    run_note_task, tid, it["video_url"], "bilibili", data.quality, data.link,
                    data.screenshot, data.model_name, data.provider_id, data.format, data.style,
                    data.extras, data.video_understanding, data.video_interval, data.grid_size, bv,
                )
                created.append({"task_id": tid, "video_id": bv, "title": it.get("title")})
            return R.success({"batch": True, "created": created, "count": len(created)})

        video_id = extract_video_id(data.video_url, data.platform)
        if (
            not video_id
            and data.platform in ("bilibili", "youtube", "douyin")
            and str(data.video_url).startswith("http")
        ):
            return R.error(msg="无法从链接中提取视频 ID，请确认粘贴的是单个视频链接", code=400)

        if data.task_id:
            # 如果传了task_id，说明是重试！
            task_id = data.task_id
            # 重试也必须去重：任务还在跑的时候再点一次「重新生成」，如果放行就会
            # 往队列里塞第二个同 task_id 的副本，并把正在跑的那次状态改写成
            # PENDING —— 前端于是整段时间都显示「排队中、第 N 位」（2026-10-01
            # 用户实拍复现）。这里直接告诉前端「已有同任务在跑」。
            if is_task_active(task_id):
                logger.info(f"重复提交拦截（重试命中运行中任务）: task_id={task_id}")
                return R.error(
                    msg="该任务正在生成中，无需重复提交",
                    code=400,
                    data={"duplicated": True, "existing_task_id": task_id, "video_id": video_id},
                )
            active_same_video = active_task_for_video(video_id) if video_id else None
            if active_same_video and active_same_video != task_id:
                logger.info(f"重复提交拦截（同视频运行中）: video_id={video_id} → {active_same_video}")
                return R.error(
                    msg="该视频已在生成队列中，请等这一次跑完",
                    code=400,
                    data={"duplicated": True, "existing_task_id": active_same_video, "video_id": video_id},
                )
            logger.info(f"重试模式，复用已有 task_id={task_id}")
        else:
            # 提交去重：同一视频已有未完成任务时不再重复建任务（防重复下载与队列膨胀）
            if video_id:
                active_tid = active_task_for_video(video_id)
                active = {"task_id": active_tid, "status": "RUNNING"} if active_tid else find_active_task_by_video(video_id)
                if active:
                    logger.info(f"重复提交拦截: video_id={video_id} 已有未完成任务 {active['task_id']}")
                    return R.error(
                        msg=f"该视频已在队列/生成中（当前状态：{active['status']}），无需重复提交",
                        code=400,
                        data={"duplicated": True, "existing_task_id": active["task_id"], "video_id": video_id},
                    )
            # 正常新建任务
            task_id = str(uuid.uuid4())

        # 统一先写入 PENDING（含 video_id/origin，供排队展示、去重与自动化任务同步）
        NoteGenerator()._update_status(
            task_id,
            TaskStatus.PENDING,
            extra={
                "video_id": video_id,
                "platform": data.platform,
                "origin": "manual",
                "video_url": str(data.video_url),
            },
        )

        # 客户端已经抓好字幕的话，写到转写缓存文件，NoteGenerator 的 cache-hit 逻辑会直接用上
        if data.prefetched_transcript:
            try:
                _persist_prefetched_transcript(task_id, data.prefetched_transcript)
            except Exception as e:
                logger.warning(f"写入预取字幕失败 (task_id={task_id}): {e}")

        background_tasks.add_task(run_note_task, task_id, data.video_url, data.platform, data.quality, data.link,
                                  data.screenshot, data.model_name, data.provider_id, data.format, data.style,
                                  data.extras, data.video_understanding, data.video_interval, data.grid_size,
                                  video_id)
        return R.success({"task_id": task_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str):
    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")

    # 优先读状态文件
    if os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8") as f:
            status_content = json.load(f)

        status = status_content.get("status")
        message = status_content.get("message", "")
        audio_meta = status_content.get("audio_meta")

        if status == TaskStatus.SUCCESS.value:
            # 成功状态的话，继续读取最终笔记内容
            if os.path.exists(result_path):
                with open(result_path, "r", encoding="utf-8") as rf:
                    result_content = json.load(rf)
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

        if status == TaskStatus.FAILED.value:
            return R.error(message or "任务失败", code=500)

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
    if os.path.exists(result_path):
        with open(result_path, "r", encoding="utf-8") as f:
            result_content = json.load(f)
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
def get_recent_tasks(limit: int = 80):
    """后端最近任务列表（扫描状态文件），供前端历史增量同步与自动化任务可见性。"""
    try:
        limit = max(1, min(int(limit), 300))
    except Exception:
        limit = 80
    return R.success({"tasks": list_recent_tasks(limit=limit)})


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str):
    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": request.headers.get("User-Agent", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="图片获取失败")

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return StreamingResponse(
                resp.aiter_bytes(),
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",  #  缓存一天
                    "Content-Type": content_type,
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

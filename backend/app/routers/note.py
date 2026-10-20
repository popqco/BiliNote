# app/routers/note.py
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel, validator, field_validator, model_validator

from app.db.video_task_dao import get_task_by_video
from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TERMINAL_STATUSES, TaskStatus
from app.exceptions.note import NoteError
from app.paths import NOTE_OUTPUT_DIR, UPLOAD_DIR
from app.services import task_state
from app.services.note import logger
from app.services.note_tasks import note_task_service
from app.services.task_serial_executor import task_serial_executor
from app.services.video_meta import fetch_video_meta
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import extract_video_id, normalize_video_url
from app.validators.video_url_validator import is_supported_video_url
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
import asyncio
import hashlib
import httpx
from PIL import Image, ImageOps

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()


def _spawn_meta_prefetch(task_id: str, video_url: str, platform: str) -> None:
    """提交后后台轻量抓一次标题/封面并合并进状态文件。

    手动单条提交不等元信息（yt-dlp 需数秒）也不阻塞响应：先写 PENDING 让卡片
    出现，元信息到手后合并刷新（前端轮询 /tasks/recent 即可显示标题/封面）。
    2026-10-13 用户实拍：手动粘贴的 YouTube 链接排了几十个，卡片全是
    「未命名笔记」。任务真正开跑后拿到的元信息更权威，会自然覆盖这份。
    """

    def _work():
        try:
            # 已有标题（重试/复用历史任务）就不再抓。root 取 services.note 的
            # NOTE_OUTPUT_DIR（历史行为：测试与运行期都以该模块属性为准）。
            from app.services.note import NOTE_OUTPUT_DIR as note_out

            cur = task_state.read_status(task_id, root=note_out) or {}
            if (cur.get("audio_meta") or {}).get("title"):
                return
            meta = fetch_video_meta(video_url, platform) or {}
            if not meta.get("title"):
                return
            task_state.merge_status_meta(
                task_id,
                {
                    "audio_meta": {
                        "title": meta.get("title"),
                        "cover_url": meta.get("cover_url"),
                        "video_id": meta.get("video_id"),
                        "duration": meta.get("duration"),
                        "platform": platform,
                    }
                },
                root=note_out,
            )
            logger.info(f"已补排队任务元信息 {task_id[:8]} ← {str(meta.get('title'))[:32]}")
        except Exception as e:
            logger.debug(f"补排队任务元信息失败（忽略，任务开跑后仍会拿到）: {e}")

    threading.Thread(target=_work, name=f"meta-{task_id[:8]}", daemon=True).start()


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


def save_note_to_file(task_id: str, note):
    """兼容入口：结果持久化已移到 NoteTaskService。"""
    note_task_service.save_note_to_file(task_id, note, output_dir=NOTE_OUTPUT_DIR)


def _persist_prefetched_transcript(task_id: str, transcript: dict) -> None:
    """兼容入口：预取字幕缓存已移到 NoteTaskService。"""
    note_task_service.persist_prefetched_transcript(
        task_id, transcript, output_dir=NOTE_OUTPUT_DIR
    )


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
                if task_state.find_active_task_by_video(bv, root=NOTE_OUTPUT_DIR):
                    continue
                tid = str(uuid.uuid4())
                task_state.write_status(
                    tid,
                    TaskStatus.PENDING,
                    extra={
                        "video_id": bv,
                        "platform": "bilibili",
                        "origin": "manual",
                        "video_url": it["video_url"],
                        "model_name": data.model_name,
                        "provider_id": data.provider_id,
                        "style": data.style,
                        "audio_meta": {
                            "title": it.get("title"),
                            "cover_url": it.get("cover_url"),
                            "video_id": bv,
                            "duration": it.get("duration"),
                            "platform": "bilibili",
                        },
                    },
                    root=NOTE_OUTPUT_DIR,
                )
                # 并发提交（2026-10-11）：原来 background_tasks 按注册顺序同步逐条
                # 跑，上一条全部完成才开始下一条，批量导入实际完全串行（2 个 worker
                # 只用得上 1 个）。改为每条一个 daemon 线程提交（与自动化调度器同款
                # 范式），线程内经 video_task_locks + 任务池排队，并发度仍受
                # max_workers 约束，不会把上游打爆。
                threading.Thread(
                    target=run_note_task,
                    args=(tid, it["video_url"], "bilibili", data.quality, data.link,
                          data.screenshot, data.model_name, data.provider_id, data.format, data.style,
                          data.extras, data.video_understanding, data.video_interval, data.grid_size, bv),
                    name=f"batch-{tid[:8]}",
                    daemon=True,
                ).start()
                created.append({"task_id": tid, "video_id": bv, "title": it.get("title")})
            return R.success({"batch": True, "created": created, "count": len(created)})

        video_id = extract_video_id(data.video_url, data.platform)
        if (
            not video_id
            and data.platform in ("bilibili", "youtube", "douyin")
            and str(data.video_url).startswith("http")
        ):
            return R.error(msg="无法从链接中提取视频 ID，请确认粘贴的是单个视频链接", code=400)

        # 提交入口参数校验：model_name/provider_id 缺了直接 400 拒绝，不要建
        # PENDING 任务再丢给后台——后台里 raise 只会炸成响应已发出的异常，
        # 状态文件永远 PENDING，前端永远"排队中"（2026-10-03 陀螺仪任务实测）。
        if not data.model_name or not data.provider_id:
            logger.warning(f"拒绝 generate_note：缺少模型参数（video_id={video_id}）")
            return R.error(msg="请选择模型和提供者后再提交", code=400)

        if data.task_id:
            # 如果传了task_id，说明是重试！
            task_id = data.task_id
            # 重试也必须去重：任务还在跑的时候再点一次「重新生成」，如果放行就会
            # 往队列里塞第二个同 task_id 的副本，并把正在跑的那次状态改写成
            # PENDING —— 前端于是整段时间都显示「排队中、第 N 位」（2026-10-01
            # 用户实拍复现）。这里直接告诉前端「已有同任务在跑」。
            if task_state.is_task_active(task_id):
                # P1-①：内存命中时先核磁盘终态——磁盘已 FAILED/SUCCESS 说明登记是
                # 僵尸（进程重启前 mark_task_done 没跑到 / finally 被跳过），清掉后
                # 继续走正常重试流程；磁盘仍非终态或文件缺失/损坏才保持 400 拦截。
                if task_state.release_stale_active_task(task_id, root=NOTE_OUTPUT_DIR):
                    logger.info(f"重试放行（僵尸登记已清理）: task_id={task_id}")
                else:
                    logger.info(f"重复提交拦截（重试命中运行中任务）: task_id={task_id}")
                    return R.error(
                        msg="该任务正在生成中，无需重复提交",
                        code=400,
                        data={"duplicated": True, "existing_task_id": task_id, "video_id": video_id},
                    )
            active_same_video = task_state.active_task_for_video(video_id) if video_id else None
            if active_same_video and active_same_video != task_id:
                # 同上：同视频的内存登记也可能是僵尸（磁盘已终态），清掉后放行；
                # 真在跑才保持 400 拦截。
                if task_state.release_stale_active_task(active_same_video, root=NOTE_OUTPUT_DIR):
                    logger.info(f"重试放行（同视频僵尸登记已清理）: video_id={video_id} → {active_same_video}")
                else:
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
                active_tid = task_state.active_task_for_video(video_id)
                active = ({"task_id": active_tid, "status": "RUNNING"} if active_tid
                          else task_state.find_active_task_by_video(video_id, root=NOTE_OUTPUT_DIR))
                if active:
                    logger.info(f"重复提交拦截: video_id={video_id} 已有未完成任务 {active['task_id']}")
                    return R.error(
                        msg=f"该视频已在队列/生成中（当前状态：{active['status']}），无需重复提交",
                        code=400,
                        data={"duplicated": True, "existing_task_id": active["task_id"], "video_id": video_id},
                    )
                # 同视频已有历史任务文件（成功或失败）时，复用最近一个 task_id：
                # 重试/重提不再开新卡，新内容追加为该任务的新版本（前端 updateTaskContent
                # 的 Markdown[] 版本链已支持）。之前这里无条件 uuid → 同一视频越刷越多张卡
                # （2026-10-01 用户投诉「重试后列表里一堆同样的视频笔记」）。
                # 自动化走 _submit_task 不经过这里，不受影响。
                existing_tids = task_state.find_task_ids_by_video(
                    video_id, data.platform, root=NOTE_OUTPUT_DIR
                )
                if existing_tids:
                    task_id = existing_tids[0]
                    logger.info(f"同视频复用历史任务: video_id={video_id} → task_id={task_id}")
                    # 复用分支走与重试相同的 PENDING 写入路径（下方统一处理）
                else:
                    # 正常新建任务
                    task_id = str(uuid.uuid4())

        # 统一先写入 PENDING（含 video_id/origin，供排队展示、去重与自动化任务同步；
        # 含 model_name/provider_id/style，供徽标与 Viewer 回填——此前没写，
        # 手动提交的笔记一旦走 /tasks/recent 回填，模型/风格徽标即空白）
        # 新提交（含重试/复用）视为对旧停止请求的作废：清掉预取消墓碑，
        # 否则「点停止后马上重试」会被上一次的停止误伤（见 services/task_runs.py）
        task_state.clear_cancel_request(task_id)
        task_state.write_status(
            task_id,
            TaskStatus.PENDING,
            extra={
                "video_id": video_id,
                "platform": data.platform,
                "origin": "manual",
                "video_url": str(data.video_url),
                "model_name": data.model_name,
                "provider_id": data.provider_id,
                "style": data.style,
                # 提交时的完整参数：状态文件落盘后，即使进程重启（reap 会把非终态
                # 任务判失败）也能凭这份记录忠实重提，不必让用户重填表单。
                "request": {
                    "quality": str(data.quality),
                    "link": bool(data.link),
                    "screenshot": bool(data.screenshot),
                    "format": list(data.format or []),
                    "extras": data.extras,
                    "video_understanding": bool(data.video_understanding),
                    "video_interval": data.video_interval,
                    "grid_size": list(data.grid_size or []),
                },
            },
            root=NOTE_OUTPUT_DIR,
        )
        # 排队卡片的标题/封面：后台轻量补一次（不阻塞提交）
        _spawn_meta_prefetch(task_id, str(data.video_url), data.platform)

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


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str):
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
    return R.success({"tasks": task_state.list_recent_tasks(limit=limit, root=NOTE_OUTPUT_DIR)})


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


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str):
    target = _normalize_image_url(url)
    if not target.startswith(("http://", "https://")):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "无效的图片地址"},
            headers={"Cache-Control": "no-store"},
        )

    key = hashlib.sha1(target.encode("utf-8")).hexdigest()
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
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    resp = await client.get(cand, headers=headers)
                if resp.status_code == 200 and resp.content:
                    ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
                    if not ctype.startswith("image/"):
                        ctype = "image/jpeg"
                    p = _store_cached_cover(key, ctype, resp.content)
                    if p is not None:
                        return FileResponse(
                            str(p),
                            media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"},
                        )
                    return Response(
                        content=resp.content,
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
    展开成 http://127.0.0.1:8483/... 两种形态都收敛到文件名解析；外链
    （http 图床）不走缩略，调用方原样返回 404 让前端回退原图。
    """
    u = _normalize_image_url(url)
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if host not in ("", "127.0.0.1", "localhost"):
        return None
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
def img_thumb(url: str, w: int = _THUMB_DEFAULT_WIDTH):
    # 同步 def：PIL 缩放是 CPU 密集操作，FastAPI 会自动丢线程池执行，
    # 不阻塞事件循环（与其他 async 端点互不影响）
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

    path = _build_thumb(src, width) or src
    media = _THUMB_MEDIA_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(
        str(path),
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400"},
    )

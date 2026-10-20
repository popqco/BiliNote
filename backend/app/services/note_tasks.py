"""笔记任务的执行门面。

`run_note_task`、结果文件持久化与预取字幕缓存原先住在 ``routers/note.py``，
导致自动化调度器必须反向 import 路由。这里把执行侧收进 service：

- ``run``：取消登记 → 活跃登记 → 同视频锁 → 任务池 → 保存结果 → 建索引；
- ``save_note_to_file`` / ``persist_prefetched_transcript``：任务产物写入。

HTTP 校验、去重与响应整形仍留在 router；它们还没有被这个门面接管。
"""
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.paths import NOTE_OUTPUT_DIR
from app.services import task_state
from app.services.note import NoteGenerator
from app.services.task_serial_executor import task_serial_executor, video_task_locks
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.services.video_meta import fetch_video_meta
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id

logger = get_logger(__name__)


class NoteTaskError(Exception):
    """提交阶段的业务错误；由 router 映射成 R.error。"""

    def __init__(self, message: str, code: int = 400, data: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


@dataclass
class SubmitResult:
    task_id: Optional[str] = None
    created: Optional[List[dict]] = None

    def as_response(self) -> dict:
        if self.created is not None:
            return {"batch": True, "created": self.created, "count": len(self.created)}
        return {"task_id": self.task_id}


class NoteTaskService:
    """任务执行与产物写入的单一入口。"""

    def __init__(self, output_dir: Optional[Path] = None):
        self._output_dir = Path(output_dir) if output_dir is not None else None

    @property
    def output_dir(self) -> Path:
        # 延迟解析：测试会 monkeypatch 模块级 NOTE_OUTPUT_DIR
        return self._output_dir or NOTE_OUTPUT_DIR

    def save_note_to_file(self, task_id: str, note, output_dir: Optional[Path] = None) -> None:
        root = Path(output_dir) if output_dir is not None else self.output_dir
        os.makedirs(root, exist_ok=True)
        with open(root / f"{task_id}.json", "w", encoding="utf-8") as f:
            json.dump(asdict(note), f, ensure_ascii=False, indent=2)

    def persist_prefetched_transcript(
        self,
        task_id: str,
        transcript: dict,
        output_dir: Optional[Path] = None,
    ) -> None:
        """把客户端预取的字幕写到 NoteGenerator 期望的转写缓存文件里。

        NoteGenerator.generate 会优先读 <task_id>_transcript.json，命中即跳过
        download_subtitles 与音频转写流程。要求字段：
        language(可空)/full_text/segments[{start,end,text}]
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

        root = Path(output_dir) if output_dir is not None else self.output_dir
        os.makedirs(root, exist_ok=True)
        target = root / f"{task_id}_transcript.json"
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"已写入客户端预取字幕缓存: {target} ({len(cleaned_segments)} 段)")

    def _spawn_meta_prefetch(
        self,
        task_id: str,
        video_url: str,
        platform: str,
        output_dir: Optional[Path] = None,
    ) -> None:
        """提交后后台轻量抓一次标题/封面并合并进状态文件。"""
        root = Path(output_dir) if output_dir is not None else self.output_dir

        def _work():
            try:
                cur = task_state.read_status(task_id, root=root) or {}
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
                    root=root,
                )
                logger.info(f"已补排队任务元信息 {task_id[:8]} ← {str(meta.get('title'))[:32]}")
            except Exception as e:
                logger.debug(f"补排队任务元信息失败（忽略，任务开跑后仍会拿到）: {e}")

        threading.Thread(target=_work, name=f"meta-{task_id[:8]}", daemon=True).start()

    @staticmethod
    def _default_schedule(fn, *args, **kwargs) -> None:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def submit(
        self,
        *,
        video_url: str,
        platform: str,
        quality: DownloadQuality,
        link: bool = False,
        screenshot: bool = False,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        formats: Optional[list] = None,
        style: Optional[str] = None,
        extras: Optional[str] = None,
        video_understanding: bool = False,
        video_interval=0,
        grid_size: Optional[List] = None,
        task_id: Optional[str] = None,
        prefetched_transcript: Optional[dict] = None,
        schedule=None,
        output_dir: Optional[Path] = None,
    ) -> SubmitResult:
        """提交一条或一批任务；业务错误抛 NoteTaskError，由 router 转信封。"""
        root = Path(output_dir) if output_dir is not None else self.output_dir

        # 就绪门禁：本地转写引擎必须等模型下载完才能跑视频。客户端已抓好字幕
        # （prefetched_transcript）则不需要转写，跳过检查。
        if not prefetched_transcript:
            readiness = TranscriberConfigManager().is_model_ready()
            if not readiness["ready"]:
                logger.warning(f"拒绝 generate_note：{readiness['reason']}")
                raise NoteTaskError(
                    readiness["reason"],
                    code=300102,
                    data={
                        "reason": "transcriber_model_not_ready",
                        "transcriber_type": readiness["transcriber_type"],
                        "model_size": readiness["model_size"],
                        "downloading": readiness["downloading"],
                    },
                )

        # 稍后再看「列表页」链接：批量导入（拉列表 → 去重 → 逐条建任务）
        from app.services.watchlater import fetch_watchlater, is_watchlater_list_url

        if is_watchlater_list_url(str(video_url)):
            try:
                items = fetch_watchlater(max_items=50)
            except Exception as e:
                raise NoteTaskError(f"获取稍后再看列表失败：{e}", code=400)
            created = []
            for it in items:
                bv = it.get("bvid")
                if not bv:
                    continue
                if task_state.find_active_task_by_video(bv, root=root):
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
                        "model_name": model_name,
                        "provider_id": provider_id,
                        "style": style,
                        "audio_meta": {
                            "title": it.get("title"),
                            "cover_url": it.get("cover_url"),
                            "video_id": bv,
                            "duration": it.get("duration"),
                            "platform": "bilibili",
                        },
                    },
                    root=root,
                )
                # 每条一个 daemon 线程提交；线程内经视频锁 + 任务池约束并发度。
                threading.Thread(
                    target=self.run,
                    args=(tid, it["video_url"], "bilibili", quality, link,
                          screenshot, model_name, provider_id, formats, style,
                          extras, video_understanding, video_interval, grid_size, bv),
                    kwargs={"output_dir": root},
                    name=f"batch-{tid[:8]}",
                    daemon=True,
                ).start()
                created.append({"task_id": tid, "video_id": bv, "title": it.get("title")})
            return SubmitResult(created=created)

        video_id = extract_video_id(str(video_url), platform)
        if (
            not video_id
            and platform in ("bilibili", "youtube", "douyin")
            and str(video_url).startswith("http")
        ):
            raise NoteTaskError("无法从链接中提取视频 ID，请确认粘贴的是单个视频链接", code=400)

        # 后台任务里的参数缺失不能等到执行阶段才暴露。
        if not model_name or not provider_id:
            logger.warning(f"拒绝 generate_note：缺少模型参数（video_id={video_id}）")
            raise NoteTaskError("请选择模型和提供者后再提交", code=400)

        if task_id:
            # 重试去重：同一个 task 正在跑时再点「重新生成」会往队列塞副本并
            # 把状态改回 PENDING。内存命中先核磁盘终态，僵尸登记清掉后放行。
            if task_state.is_task_active(task_id):
                if task_state.release_stale_active_task(task_id, root=root):
                    logger.info(f"重试放行（僵尸登记已清理）: task_id={task_id}")
                else:
                    logger.info(f"重复提交拦截（重试命中运行中任务）: task_id={task_id}")
                    raise NoteTaskError(
                        "该任务正在生成中，无需重复提交",
                        code=400,
                        data={"duplicated": True, "existing_task_id": task_id, "video_id": video_id},
                    )
            active_same_video = task_state.active_task_for_video(video_id) if video_id else None
            if active_same_video and active_same_video != task_id:
                if task_state.release_stale_active_task(active_same_video, root=root):
                    logger.info(f"重试放行（同视频僵尸登记已清理）: video_id={video_id} → {active_same_video}")
                else:
                    logger.info(f"重复提交拦截（同视频运行中）: video_id={video_id} → {active_same_video}")
                    raise NoteTaskError(
                        "该视频已在生成队列中，请等这一次跑完",
                        code=400,
                        data={"duplicated": True, "existing_task_id": active_same_video, "video_id": video_id},
                    )
            logger.info(f"重试模式，复用已有 task_id={task_id}")
        else:
            # 提交去重：同一视频已有未完成任务时不再重复建任务。
            if video_id:
                active_tid = task_state.active_task_for_video(video_id)
                active = ({"task_id": active_tid, "status": "RUNNING"} if active_tid
                          else task_state.find_active_task_by_video(video_id, root=root))
                if active:
                    logger.info(f"重复提交拦截: video_id={video_id} 已有未完成任务 {active['task_id']}")
                    raise NoteTaskError(
                        f"该视频已在队列/生成中（当前状态：{active['status']}），无需重复提交",
                        code=400,
                        data={"duplicated": True, "existing_task_id": active["task_id"], "video_id": video_id},
                    )
                # 同视频已有历史任务文件（成功或失败）时，复用最近一个 task_id：
                # 重试/重提不再开新卡，新内容追加为该任务的新版本。
                existing_tids = task_state.find_task_ids_by_video(video_id, platform, root=root)
                if existing_tids:
                    task_id = existing_tids[0]
                    logger.info(f"同视频复用历史任务: video_id={video_id} → task_id={task_id}")
                else:
                    task_id = str(uuid.uuid4())

        # 新提交（含重试/复用）视为对旧停止请求的作废：清掉预取消墓碑。
        task_state.clear_cancel_request(task_id)
        task_state.write_status(
            task_id,
            TaskStatus.PENDING,
            extra={
                "video_id": video_id,
                "platform": platform,
                "origin": "manual",
                "video_url": str(video_url),
                "model_name": model_name,
                "provider_id": provider_id,
                "style": style,
                # 提交时的完整参数：进程重启后仍能凭这份记录忠实重提。
                "request": {
                    "quality": str(quality),
                    "link": bool(link),
                    "screenshot": bool(screenshot),
                    "format": list(formats or []),
                    "extras": extras,
                    "video_understanding": bool(video_understanding),
                    "video_interval": video_interval,
                    "grid_size": list(grid_size or []),
                },
            },
            root=root,
        )
        self._spawn_meta_prefetch(task_id, str(video_url), platform, output_dir=root)

        if prefetched_transcript:
            try:
                self.persist_prefetched_transcript(
                    task_id, prefetched_transcript, output_dir=root
                )
            except Exception as e:
                logger.warning(f"写入预取字幕失败 (task_id={task_id}): {e}")

        schedule = schedule or self._default_schedule
        schedule(
            self.run, task_id, str(video_url), platform, quality, link, screenshot,
            model_name, provider_id, formats, style, extras, video_understanding,
            video_interval, grid_size, video_id,
            output_dir=root,
        )
        return SubmitResult(task_id=task_id)

    def run(
        self,
        task_id: str,
        video_url: str,
        platform: str,
        quality: DownloadQuality,
        link: bool = False,
        screenshot: bool = False,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        _format: Optional[list] = None,
        style: Optional[str] = None,
        extras: Optional[str] = None,
        video_understanding: bool = False,
        video_interval=0,
        grid_size: Optional[List] = None,
        video_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> None:
        """执行一次笔记任务；所有失败都收敛进状态文件，不向上抛 HTTP 异常。"""
        root = Path(output_dir) if output_dir is not None else self.output_dir
        grid_size = grid_size or []

        # 后台任务里的参数缺失不能 raise HTTPException——响应早就发出去了，
        # 异常只会炸成 "Caught handled exception, but response already started"，
        # 状态文件永远停在 PENDING，前端永远"排队中"（2026-10-03 陀螺仪任务实测）。
        if not model_name or not provider_id:
            logger.warning(f"任务 {task_id} 缺少模型参数（model_name={model_name!r}），直接标失败")
            task_state.write_status(
                task_id,
                TaskStatus.FAILED,
                message="HTTPException: 请选择模型和提供者（提交时未带模型参数，请重新选择模型后生成）",
                root=root,
            )
            return

        def _execute_note_task():
            # 排队中被停止：先查取消，避免为了写一个 CANCELLED 又去初始化转写器
            # （首次构建实测 10s+，排队任务点了停止还要等这么久）。
            if task_state.is_run_cancelled(run):
                task_state.write_status(
                    task_id,
                    TaskStatus.CANCELLED,
                    message="已停止生成（排队中取消）",
                    root=root,
                )
                return None
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
                run=run,
            )

        logger.info(f"任务进入执行队列 (task_id={task_id})")
        # 取消登记先于入池：排队中的任务也要能停。
        run = task_state.register_run(task_id)
        # 登记「本进程正在跑」：删除接口靠它拒绝删正在生成的任务，重复提交靠它
        # 拦住同一个 task 被点两次重新生成。token=run：停止后立刻重试时，旧 run
        # 的收尾摘除不会误摘新 run 的登记。
        task_state.mark_task_active(task_id, video_id, token=run)
        try:
            # 同一视频互斥：防止同视频双任务并发下载/生成触发文件锁冲突
            # （WinError 32）。不同视频互不阻塞（ADR-0001）。
            with video_task_locks.get(video_id or task_id):
                note = task_serial_executor.run(_execute_note_task, task_id=task_id)
        finally:
            task_state.mark_task_done(task_id, token=run)
            task_state.finish_run(run)

        logger.info(f"Note generated: {task_id}")
        if not note or not note.markdown:
            logger.warning(f"任务 {task_id} 执行失败，跳过保存")
            return
        self.save_note_to_file(task_id, note, output_dir=root)

        # 自动建立向量索引（用于 AI 问答），失败不影响笔记生成
        try:
            from app.services.vector_store import VectorStoreManager

            VectorStoreManager().index_task(task_id)
        except Exception as e:
            logger.warning(f"向量索引失败（不影响笔记）: {e}")


note_task_service = NoteTaskService()

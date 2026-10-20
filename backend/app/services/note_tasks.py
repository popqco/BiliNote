"""笔记任务的执行门面。

`run_note_task`、结果文件持久化与预取字幕缓存原先住在 ``routers/note.py``，
导致自动化调度器必须反向 import 路由。这里把执行侧收进 service：

- ``run``：取消登记 → 活跃登记 → 同视频锁 → 任务池 → 保存结果 → 建索引；
- ``save_note_to_file`` / ``persist_prefetched_transcript``：任务产物写入。

HTTP 校验、去重与响应整形仍留在 router；它们还没有被这个门面接管。
"""
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.paths import NOTE_OUTPUT_DIR
from app.services import task_state
from app.services.note import NoteGenerator
from app.services.task_serial_executor import task_serial_executor, video_task_locks
from app.utils.logger import get_logger

logger = get_logger(__name__)


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

"""「停止正在生成的任务」回归测试（2026-10-16 用户需求）。

场景：模型/参数设置错了时，任务会一直跑（还带转写重试、总结降级阶梯等
「不停地尝试」），用户只能干等。本套测试覆盖：

1. run 登记表的语义：取消、旧 run 作废（重试场景）、提交窗口期的预取消墓碑、
   墓碑被新提交清除；
2. 状态写入抑制：已取消的 run 不能把 CANCELLED 覆盖回进度；被作废的
   run（同 task_id 已重试）任何写入都被忽略；
3. 总结阶段的取消：阶梯不把 TaskCancelled 当成「这一级失败」继续降级，
   终态落 CANCELLED 而不是 FAILED；
4. 转写重试的退避期间取消：不必等退避睡满；
5. 排队中的任务也能停（run 在进线程池之前就登记）；
6. /cancel_task 端点：live 取消落盘、已结束任务如实返回并清墓碑、
   取消后重试不被墓碑误伤。
"""

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.enmus.task_status_enums import TaskStatus  # noqa: E402
from app.exceptions.task_cancel import TaskCancelled  # noqa: E402
from app.models.transcriber_model import TranscriptResult, TranscriptSegment  # noqa: E402
from app.services.task_state import runs as task_runs  # noqa: E402
from app.services.task_serial_executor import ConcurrentTaskExecutor  # noqa: E402
import app.services.note as note_svc  # noqa: E402
from app.services.note import NoteGenerator  # noqa: E402


def _body(resp):
    """R.success/R.error 返回 JSONResponse；取出信封 data 字段。"""
    if hasattr(resp, "body"):
        return json.loads(resp.body.decode("utf-8")).get("data") or {}
    return resp.get("data") or {}


def _read_status(tmpdir, task_id):
    p = pathlib.Path(tmpdir) / f"{task_id}.status.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


class _StubDownloader:
    """字幕/元信息都本地给：不碰网络的下载器替身。"""

    cancel_check = None

    def __init__(self):
        self.download_calls = 0
        self.video_download_calls = 0

    def download_subtitles(self, video_url, output_dir=None, langs=None):
        return TranscriptResult(
            language="zh",
            full_text="测试文本",
            segments=[TranscriptSegment(start=0.0, end=1.0, text="测试文本")],
        )

    def download(self, video_url, output_dir=None, quality="fast", need_video=False, skip_download=False):
        from app.models.notes_model import AudioDownloadResult

        self.download_calls += 1
        return AudioDownloadResult(
            file_path=None, title="测试视频", duration=10, cover_url=None,
            platform="bilibili", video_id="BVtest", raw_info={}, video_path=None,
        )

    def download_video(self, video_url, output_dir=None):
        self.video_download_calls += 1
        raise AssertionError("取消后不应下载视频")


class _BlockingSummarizeGPT:
    """summarize 阻塞到被取消：模拟长总结/降级阶梯中用户点停止。"""

    cancel_check = None

    def __init__(self, hold_seconds=5.0):
        self.calls = 0
        self.hold_seconds = hold_seconds
        self.entered = threading.Event()

    def summarize(self, source):
        self.calls += 1
        self.entered.set()
        deadline = time.time() + self.hold_seconds
        while time.time() < deadline:
            # 与 UniversalGPT 同款：流式/循环内部查取消谓词
            if self.cancel_check and self.cancel_check():
                raise TaskCancelled()
            time.sleep(0.02)
        raise AssertionError("GPT 未被取消，测试阻塞超时")

    def create_messages(self, segments, **kwargs):
        return []


class TestTaskRunRegistry(unittest.TestCase):
    def setUp(self):
        task_runs.reset_for_tests()

    def test_request_cancel_sets_event_on_live_run(self):
        run = task_runs.register_run("t-live")
        self.assertTrue(task_runs.request_cancel_task("t-live"))
        self.assertTrue(task_runs.is_run_cancelled(run))

    def test_register_run_supersedes_old_run(self):
        old = task_runs.register_run("t-1")
        new = task_runs.register_run("t-1")
        self.assertTrue(old.superseded)
        self.assertTrue(task_runs.is_run_cancelled(old), "旧 run 必须同时被打上取消标记")
        self.assertFalse(new.cancelled, "新 run 不该继承取消状态")
        self.assertIs(task_runs.current_run("t-1"), new)

    def test_finish_old_run_does_not_pop_new(self):
        old = task_runs.register_run("t-2")
        new = task_runs.register_run("t-2")
        task_runs.finish_run(old)
        self.assertIs(task_runs.current_run("t-2"), new, "旧 run 收尾不许摘掉新 run 的登记")
        task_runs.finish_run(new)
        self.assertIsNone(task_runs.current_run("t-2"))

    def test_pre_cancel_tombstone_applies_to_late_run(self):
        """提交与线程启动之间点停止：晚到的 run 直接以已取消状态诞生。"""
        self.assertFalse(task_runs.request_cancel_task("t-late"))
        run = task_runs.register_run("t-late")
        self.assertTrue(run.cancelled)

    def test_clear_cancel_request_resets_tombstone(self):
        task_runs.request_cancel_task("t-retry")
        task_runs.clear_cancel_request("t-retry")
        run = task_runs.register_run("t-retry")
        self.assertFalse(run.cancelled, "新提交不该被上一次的停止请求误伤")

    def test_is_task_cancel_requested_covers_both(self):
        task_runs.register_run("t-a")
        task_runs.request_cancel_task("t-a")
        self.assertTrue(task_runs.is_task_cancel_requested("t-a"))
        task_runs.finish_run(task_runs.current_run("t-a"))
        task_runs.request_cancel_task("t-b")  # 只有墓碑
        self.assertTrue(task_runs.is_task_cancel_requested("t-b"))
        self.assertFalse(task_runs.is_task_cancel_requested("t-c"))


class TestStatusWriteSuppression(unittest.TestCase):
    def setUp(self):
        task_runs.reset_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self._old_out = note_svc.NOTE_OUTPUT_DIR
        note_svc.NOTE_OUTPUT_DIR = pathlib.Path(self.tmp.name)

    def tearDown(self):
        note_svc.NOTE_OUTPUT_DIR = self._old_out
        self.tmp.cleanup()

    def _gen(self, run):
        gen = object.__new__(NoteGenerator)  # 绕开 __init__（不需要真转写器）
        gen._run = run
        return gen

    def test_cancelled_run_cannot_write_progress(self):
        run = task_runs.register_run("t-suppress")
        gen = self._gen(run)
        gen._update_status("t-suppress", TaskStatus.PARSING)
        task_runs.request_cancel_task("t-suppress")
        # 取消后迟到的阶段推进不许覆盖「已停止」
        gen._update_status("t-suppress", TaskStatus.SUMMARIZING)
        self.assertEqual(_read_status(self.tmp.name, "t-suppress")["status"], "PARSING")
        # CANCELLED 本身允许写（收尾文案）
        gen._update_status("t-suppress", TaskStatus.CANCELLED, "已停止生成（用户手动停止）")
        data = _read_status(self.tmp.name, "t-suppress")
        self.assertEqual(data["status"], "CANCELLED")
        self.assertIn("已停止", data["message"])

    def test_superseded_run_writes_nothing(self):
        old = task_runs.register_run("t-sup")
        gen = self._gen(old)
        task_runs.register_run("t-sup")  # 重试：新 run 接管
        gen._update_status("t-sup", TaskStatus.CANCELLED, "旧 run 的收尾")
        self.assertIsNone(_read_status(self.tmp.name, "t-sup"), "被作废的 run 不许写任何状态")

    def test_unbound_generator_always_writes(self):
        """路由提交时的 PENDING 写入用的是没绑 run 的 NoteGenerator，不受抑制。"""
        task_runs.register_run("t-router")
        gen = self._gen(None)
        gen._update_status("t-router", TaskStatus.PENDING)
        self.assertEqual(_read_status(self.tmp.name, "t-router")["status"], "PENDING")


class TestCancelDuringGenerate(unittest.TestCase):
    def setUp(self):
        task_runs.reset_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self._old_out = note_svc.NOTE_OUTPUT_DIR
        note_svc.NOTE_OUTPUT_DIR = pathlib.Path(self.tmp.name)
        self.downloader = _StubDownloader()
        self.gpt = _BlockingSummarizeGPT()

        self._patches = [
            patch.object(NoteGenerator, "_get_downloader", lambda self, platform: self._stub_dl),
            patch.object(NoteGenerator, "_get_gpt", lambda self, m, p: self._stub_gpt),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        note_svc.NOTE_OUTPUT_DIR = self._old_out
        self.tmp.cleanup()

    def _make_generator(self):
        gen = object.__new__(NoteGenerator)
        gen.model_size = "tiny"
        gen.device = None
        gen.transcriber_type = "fast-whisper"
        gen.transcriber = None
        gen.video_path = None
        gen.video_img_urls = []
        gen._last_frame_interval = None
        gen._last_grid_size = None
        gen._run = None
        gen._stub_dl = self.downloader
        gen._stub_gpt = self.gpt
        return gen

    def test_cancel_during_summarize_lands_cancelled_not_failed(self):
        """总结阶段（含降级阶梯）取消：终态 CANCELLED，且阶梯不再继续下一级。"""
        task_id = "t-cancel-sum"
        gen = self._make_generator()
        run = task_runs.register_run(task_id)
        result_holder = {}

        def _worker():
            result_holder["result"] = gen.generate(
                video_url="https://www.bilibili.com/video/BVtest",
                platform="bilibili",
                task_id=task_id,
                model_name="m",
                provider_id="p",
                run=run,
            )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        self.assertTrue(self.gpt.entered.wait(5), "总结阶段没有按预期开始")
        # 用户点「停止生成」
        self.assertTrue(task_runs.request_cancel_task(task_id))
        t.join(10)
        self.assertFalse(t.is_alive(), "取消后 generate 应立即退出")

        self.assertIsNone(result_holder["result"])
        data = _read_status(self.tmp.name, task_id)
        self.assertEqual(data["status"], "CANCELLED")
        self.assertNotIn("FAILED", data["status"])
        self.assertIn("已停止", data["message"])
        self.assertEqual(self.gpt.calls, 1, "取消不该触发总结降级阶梯的下一级")
        self.assertEqual(self.downloader.video_download_calls, 0)

    def test_stop_before_start_aborts_at_first_checkpoint(self):
        """预取消墓碑：晚到的执行在第一个检查点直接取消，不碰下载与模型。"""
        task_id = "t-preset"
        task_runs.request_cancel_task(task_id)  # 提交后立刻点停止
        gen = self._make_generator()
        result = gen.generate(
            video_url="https://www.bilibili.com/video/BVtest",
            platform="bilibili",
            task_id=task_id,
            model_name="m",
            provider_id="p",
        )
        self.assertIsNone(result)
        self.assertEqual(self.gpt.calls, 0)
        self.assertEqual(_read_status(self.tmp.name, task_id)["status"], "CANCELLED")

    def test_transcribe_backoff_is_interruptible(self):
        """转写重试的退避期间点停止：下一次切片检查立即抛 TaskCancelled。"""
        task_id = "t-transcribe"
        gen = self._make_generator()
        run = task_runs.register_run(task_id)
        gen._run = run

        class _AlwaysFailTranscriber:
            def __init__(self):
                self.calls = 0

            def transcript(self, file_path):
                self.calls += 1
                raise ConnectionError("connection error")

        gen.transcriber = _AlwaysFailTranscriber()
        gen.transcriber_type = "groq"  # 走在线信号量，避免与本地信号量纠缠

        audio = pathlib.Path(self.tmp.name) / "audio.mp3"
        audio.write_bytes(b"\0" * 16)

        cancel_started = {"done": False}

        def _fake_sleep(_seconds):
            # 第一次退避刚开始：用户点停止
            if not cancel_started["done"]:
                cancel_started["done"] = True
                task_runs.request_cancel_task(task_id)

        with patch.object(note_svc.time, "sleep", _fake_sleep):
            with self.assertRaises(TaskCancelled):
                gen._transcribe_audio(
                    audio_file=str(audio),
                    transcript_cache_file=pathlib.Path(self.tmp.name) / f"{task_id}_transcript.json",
                    status_phase=TaskStatus.TRANSCRIBING,
                    task_id=task_id,
                )
        self.assertEqual(gen.transcriber.calls, 1, "取消后不许再发起下一次转写尝试")


class TestCancelQueuedTaskAndEndpoint(unittest.TestCase):
    def setUp(self):
        task_runs.reset_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self._old_out = note_svc.NOTE_OUTPUT_DIR
        note_svc.NOTE_OUTPUT_DIR = pathlib.Path(self.tmp.name)
        import app.routers.note as note_router

        self.router = note_router
        self._old_router_out = note_router.NOTE_OUTPUT_DIR
        note_router.NOTE_OUTPUT_DIR = self.tmp.name

    def tearDown(self):
        note_svc.NOTE_OUTPUT_DIR = self._old_out
        self.router.NOTE_OUTPUT_DIR = self._old_router_out
        task_runs.reset_for_tests()
        self.tmp.cleanup()

    def _stub_generator_factory(self):
        class _StubGen:
            def __init__(self, *a, **kw):
                pass

            def _update_status(self, task_id, status, message=None, extra=None, **kw):
                path = pathlib.Path(self_outer.tmp.name) / f"{task_id}.status.json"
                data = {}
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                data["status"] = status.value if hasattr(status, "value") else status
                if message:
                    data["message"] = message
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self_outer = self
        return _StubGen

    def test_cancel_live_task_writes_cancelled(self):
        task_id = "t-endpoint-live"
        run = task_runs.register_run(task_id)
        gen_stub = self._stub_generator_factory()
        with patch.object(self.router, "NoteGenerator", gen_stub):
            resp = self.router.cancel_task(self.router.RecordRequest(task_id=task_id))
        self.assertTrue(_body(resp)["cancelled"])
        self.assertTrue(task_runs.is_run_cancelled(run), "live run 必须收到取消信号")
        self.assertEqual(_read_status(self.tmp.name, task_id)["status"], "CANCELLED")

    def test_cancel_finished_task_is_noop_and_clears_tombstone(self):
        task_id = "t-endpoint-done"
        (pathlib.Path(self.tmp.name) / f"{task_id}.status.json").write_text(
            json.dumps({"status": "SUCCESS"}), encoding="utf-8"
        )
        gen_stub = self._stub_generator_factory()
        with patch.object(self.router, "NoteGenerator", gen_stub):
            resp = self.router.cancel_task(self.router.RecordRequest(task_id=task_id))
        self.assertFalse(_body(resp)["cancelled"])
        # 已结束任务的停止请求不许留下墓碑（否则将来重试会被误伤）
        run = task_runs.register_run(task_id)
        self.assertFalse(run.cancelled)

    def test_cancel_orphan_nonterminal_status_is_reaped(self):
        """磁盘非终态但没有 live run（进程重启残留）：停止照常收敛为 CANCELLED。"""
        task_id = "t-endpoint-orphan"
        (pathlib.Path(self.tmp.name) / f"{task_id}.status.json").write_text(
            json.dumps({"status": "SUMMARIZING"}), encoding="utf-8"
        )
        gen_stub = self._stub_generator_factory()
        with patch.object(self.router, "NoteGenerator", gen_stub):
            resp = self.router.cancel_task(self.router.RecordRequest(task_id=task_id))
        self.assertTrue(_body(resp)["cancelled"])
        self.assertEqual(_read_status(self.tmp.name, task_id)["status"], "CANCELLED")

    def test_queued_task_cancel_aborts_without_touching_media(self):
        """排队中的任务停止：等 worker 取到它时第一个检查点即取消，不下载不调模型。"""
        old_executor = self.router.task_serial_executor
        self.router.task_serial_executor = ConcurrentTaskExecutor(max_workers=1)
        downloader = _StubDownloader()
        gpt = _BlockingSummarizeGPT()
        try:
            # 用一个占位任务占住唯一的 worker
            blocker_release = threading.Event()
            blocker = threading.Thread(
                target=lambda: self.router.task_serial_executor.run(
                    blocker_release.wait, task_id="blocker"
                ),
                daemon=True,
            )
            blocker.start()
            blocker_release.wait(0.2)  # 真实等待（不走 time.sleep，便于与补丁共存）

            task_id = "t-queued"
            with patch.object(NoteGenerator, "_get_downloader", lambda self, platform: downloader), \
                 patch.object(NoteGenerator, "_get_gpt", lambda self, m, p: gpt):
                worker = threading.Thread(
                    target=self.router.run_note_task,
                    args=(task_id, "https://www.bilibili.com/video/BVtest", "bilibili", "medium"),
                    kwargs={"model_name": "m", "provider_id": "p", "video_id": "BVtest"},
                    daemon=True,
                )
                worker.start()
                # 等到 run 已登记（排队中）再点停止
                deadline = time.time() + 5
                while time.time() < deadline and task_runs.current_run(task_id) is None:
                    time.sleep(0.02)
                self.assertIsNotNone(task_runs.current_run(task_id), "任务应在队列中完成登记")
                resp = self.router.cancel_task(self.router.RecordRequest(task_id=task_id))
                self.assertTrue(_body(resp)["cancelled"])
                blocker_release.set()  # 放行被占住的 worker
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(_read_status(self.tmp.name, task_id)["status"], "CANCELLED")
            self.assertEqual(downloader.download_calls, 0)
            self.assertEqual(downloader.video_download_calls, 0)
            self.assertEqual(gpt.calls, 0)
            # 停止后登记必须清干净：同 task 的重试不被内存门禁拦住
            self.assertFalse(note_svc.is_task_active(task_id))
        finally:
            self.router.task_serial_executor = old_executor


if __name__ == "__main__":
    unittest.main()

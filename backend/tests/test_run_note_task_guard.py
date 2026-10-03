"""后台 run_note_task 缺参数不再 raise（响应已发出，raise 只会炸成
"Caught handled exception" 且任务永远 PENDING），而是写 FAILED 并返回。

回归覆盖 2026-10-03 陀螺仪任务：手机上"重新生成"没带模型参数，
后台 raise → 状态文件永远 PENDING → 前端永远"排队中"。
"""

import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{rel} module spec not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRunNoteTaskMissingParams(unittest.TestCase):
    def test_missing_model_marks_failed_not_raise(self):
        note_router = _load("note_router_under_test", "app/routers/note.py")
        with tempfile.TemporaryDirectory() as tmp:
            # run_note_task 内部用的是 routers/note.py import 进来的那个
            # services.note 模块对象（与直接 _load services/note.py 不是同一对象，
            # 各自 exec 一次）——必须改 router 引用的那个模块的 NOTE_OUTPUT_DIR。
            import sys
            svc_mod = sys.modules.get("app.services.note")
            if svc_mod is None:
                # 回退：从 router 的符号表里找 NoteGenerator 类的定义模块
                import inspect
                svc_mod = inspect.getmodule(note_router.NoteGenerator)
            assert svc_mod is not None, "找不到 services.note 模块对象"
            old = svc_mod.NOTE_OUTPUT_DIR
            svc_mod.NOTE_OUTPUT_DIR = pathlib.Path(tmp)
            try:
                task_id = "probe-task-no-model"
                # 缺 model_name + provider_id：必须不抛异常，且状态落 FAILED
                result = note_router.run_note_task(
                    task_id, "https://www.bilibili.com/video/BV1GFa667Epf",
                    "bilibili", "medium",
                )
                self.assertIsNone(result)
                status_file = pathlib.Path(tmp) / f"{task_id}.status.json"
                self.assertTrue(status_file.is_file(), "缺参数必须留下 FAILED 状态文件")
                data = json.loads(status_file.read_text(encoding="utf-8"))
                self.assertEqual(data.get("status"), "FAILED")
                self.assertIn("请选择模型和提供者", data.get("message", ""))
            finally:
                svc_mod.NOTE_OUTPUT_DIR = old

    def test_groq_default_model_not_none(self):
        src = (ROOT / "app" / "transcriber" / "groq.py").read_text(encoding="utf-8")
        # GROQ_TRANSCRIBER_MODEL 未配置时必须有缺省，不能把 None 传给 Groq
        # （None → 400 invalid_model → 转写失败 → 任务 FAILED）
        self.assertIn("or 'whisper-large-v3-turbo'", src)


if __name__ == "__main__":
    unittest.main()

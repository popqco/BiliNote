import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_ffmpeg_helper():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None

    app_module = types.ModuleType("app")
    utils_module = types.ModuleType("app.utils")
    logger_module = types.ModuleType("app.utils.logger")

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    logger_module.get_logger = lambda *args, **kwargs: Logger()
    module_path = Path(__file__).resolve().parents[1] / "ffmpeg_helper.py"
    spec = importlib.util.spec_from_file_location("ffmpeg_helper_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "dotenv": dotenv,
            "app": app_module,
            "app.utils": utils_module,
            "app.utils.logger": logger_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


ffmpeg_helper = load_ffmpeg_helper()


class FfmpegHelperPathTests(unittest.TestCase):
    def test_check_ffmpeg_exists_does_not_duplicate_path(self):
        original_env = os.environ.copy()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["FFMPEG_BIN_PATH"] = tmpdir
                os.environ["PATH"] = r"C:\Windows\System32"
                completed = subprocess.CompletedProcess(
                    args=["ffmpeg", "-version"], returncode=0
                )
                with patch.object(ffmpeg_helper.subprocess, "run", return_value=completed):
                    for _ in range(5):
                        self.assertTrue(ffmpeg_helper.check_ffmpeg_exists())

                entries = os.environ["PATH"].split(os.pathsep)
                normalized = os.path.normcase(os.path.normpath(tmpdir))
                matching = [
                    entry for entry in entries
                    if entry and os.path.normcase(os.path.normpath(entry)) == normalized
                ]
                self.assertEqual(len(matching), 1)
        finally:
            os.environ.clear()
            os.environ.update(original_env)


if __name__ == "__main__":
    unittest.main()


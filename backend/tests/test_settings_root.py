"""配置根（JsonFileStore）与转写就绪检查的回归测试。"""
import inspect
import json

from app.config.json_store import JsonFileStore
from app.services import transcriber_config_manager as tcm_module
from app.services.automation_config_manager import (
    DEFAULT_CONFIG as AUTOMATION_DEFAULTS,
    AutomationConfigManager,
)
from app.services.task_config_manager import TaskConfigManager
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.transcriber import model_download_state, model_readiness


def test_corrupt_json_reads_empty(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert JsonFileStore(path).read() == {}


def test_atomic_write_leaves_valid_json_and_no_tmp(tmp_path):
    path = tmp_path / "store.json"
    store = JsonFileStore(path)
    store.write({"a": 1, "nested": {"b": 2}})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "nested": {"b": 2}}
    assert list(tmp_path.glob("*.tmp")) == []


def test_update_deep_merges_nested_objects(tmp_path):
    path = tmp_path / "store.json"
    store = JsonFileStore(path, {"notify": {"smtp": {"host": "", "port": 465}}})
    merged = store.update({"notify": {"smtp": {"host": "smtp.example.com"}}})

    assert merged["notify"]["smtp"] == {"host": "smtp.example.com", "port": 465}
    assert store.read()["notify"]["smtp"]["port"] == 465


def test_task_config_precedence_file_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MAX_WORKERS", "7")
    manager = TaskConfigManager(str(tmp_path / "task.json"))

    assert manager.get("max_workers") == 7  # 文件缺失：环境变量覆盖默认
    manager.update_config({"max_workers": 3})
    assert manager.get("max_workers") == 3  # 文件值优先于环境变量


def test_task_config_clamps_out_of_range(tmp_path):
    manager = TaskConfigManager(str(tmp_path / "task.json"))
    manager.update_config({"max_workers": 999, "frame_budget": -1})

    assert manager.get("max_workers") == 16
    assert manager.get("frame_budget") == 100


def test_automation_deep_merge_keeps_defaults(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(
        json.dumps({"notify": {"smtp": {"host": "smtp.example.com"}}}),
        encoding="utf-8",
    )

    config = AutomationConfigManager(str(path)).get_config()

    assert config["notify"]["smtp"]["host"] == "smtp.example.com"
    assert config["notify"]["smtp"]["port"] == AUTOMATION_DEFAULTS["notify"]["smtp"]["port"]
    assert config["interval_minutes"] == AUTOMATION_DEFAULTS["interval_minutes"]


def test_readiness_false_when_model_missing(tmp_path, monkeypatch):
    manager = TranscriberConfigManager(str(tmp_path / "transcriber.json"))
    manager.update_config("fast-whisper", "tiny")
    monkeypatch.setattr(model_readiness, "check_whisper_model_exists", lambda *a, **k: False)
    monkeypatch.setattr(model_download_state, "is_downloading", lambda key: False)

    result = manager.is_model_ready()

    assert result["ready"] is False
    assert result["downloading"] is False
    assert "尚未下载" in result["reason"]


def test_readiness_true_when_model_present(tmp_path, monkeypatch):
    manager = TranscriberConfigManager(str(tmp_path / "transcriber.json"))
    manager.update_config("fast-whisper", "tiny")
    monkeypatch.setattr(model_readiness, "check_whisper_model_exists", lambda *a, **k: True)

    assert manager.is_model_ready()["ready"] is True


def test_readiness_reports_downloading(tmp_path, monkeypatch):
    manager = TranscriberConfigManager(str(tmp_path / "transcriber.json"))
    manager.update_config("fast-whisper", "tiny")
    monkeypatch.setattr(model_readiness, "check_whisper_model_exists", lambda *a, **k: False)
    monkeypatch.setattr(model_download_state, "is_downloading", lambda key: key == "tiny")

    result = manager.is_model_ready()

    assert result["ready"] is False
    assert result["downloading"] is True
    assert "正在下载" in result["reason"]


def test_readiness_true_for_online_engine(tmp_path):
    manager = TranscriberConfigManager(str(tmp_path / "transcriber.json"))
    manager.update_config("groq")

    assert manager.is_model_ready()["ready"] is True


def test_transcriber_config_does_not_import_routers():
    source = inspect.getsource(tcm_module)
    assert "app.routers" not in source

"""2026-10-11 功能批：性能/预算配置、逐级升格阶梯、分批总结并发、思考强度。

- TaskConfigManager：默认值 / 环境变量回退 / 文件优先 / 夹取；
- _rebuild_video_grids 三种 mode（grid2/grid3/thinned）的参数与帧复用；
- _summarize_text 升格优先 5 级阶梯的级次顺序与中间级跳过；
- summarize 分批并发：乱序完成按索引归位、checkpoint 只落连续前缀；
- 供应商级 thinking_effort：chat 链路注入 + 去参重试、responses 映射、工厂透传；
- thinking_effort 归属迁移（2026-10-11 定版）：改挂模型级——_get_gpt 只读 models 行、
  供应商遗留值不回读、init_db 一次性把供应商值搬迁到其模型行；
- ConcurrentTaskExecutor.set_max_workers 热换池；
- VideoReader.run(reuse_frames=True) 跳过重新抽帧 + 并行拼图/编码保序。
"""
import base64
import json
import os
import re
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from app.gpt.gpt_factory import GPTFactory
from app.gpt.provider.responses_compat import _Completions
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig
from app.models.transcriber_model import TranscriptSegment
from app.services import note as note_mod
from app.services.note import NoteGenerator
from app.services.task_config_manager import TaskConfigManager
from app.services.task_serial_executor import ConcurrentTaskExecutor
from app.utils.video_reader import VideoReader

# 注意：全量跑时 test_cross_note_qa 会把 sys.modules["app"] 换成空壳桩且不恢复，
# 所以本文件里所有 monkeypatch 一律打在「文件顶部 import 到手的对象」上，
# 禁止用 "app.services.xxx.Attr" 字符串路径（pytest 会现场 __import__ 撞桩）。
# 另外 test_video_reader_dedupe 收集期会把 sys.modules["PIL"] 永久换成假包——
# 之后任何惰性插件注册（Image.preinit/init 的 from PIL import XxxPlugin）都会
# 静默 ImportError，导致真 PIL 的 .jpg 保存报 unknown file extension。
# 趁本文件收集期（按字母序在假包替换之前）把真插件全部注册死。
PILImage.init()


# ---------------------------------------------------------------------------
# TaskConfigManager
# ---------------------------------------------------------------------------

def test_task_config_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_MAX_WORKERS", raising=False)
    monkeypatch.delenv("FRAME_BUDGET", raising=False)
    monkeypatch.delenv("IMAGE_BUDGET", raising=False)
    monkeypatch.delenv("SUMMARIZE_BATCH_CONCURRENCY", raising=False)
    mgr = TaskConfigManager(filepath=str(tmp_path / "task_config.json"))
    cfg = mgr.get_config()
    assert cfg == {
        "max_workers": 2,
        "frame_budget": 4800,
        "image_budget": 1000,
        "summarize_batch_concurrency": 2,
    }
    # 读不落盘：文件缺失时环境变量/默认值照常工作
    assert not (tmp_path / "task_config.json").exists()


def test_task_config_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MAX_WORKERS", "4")
    mgr = TaskConfigManager(filepath=str(tmp_path / "task_config.json"))
    assert mgr.get("max_workers") == 4


def test_task_config_file_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAME_BUDGET", "4800")
    p = tmp_path / "task_config.json"
    p.write_text(json.dumps({"frame_budget": 600}), encoding="utf-8")
    mgr = TaskConfigManager(filepath=str(p))
    assert mgr.get("frame_budget") == 600


def test_task_config_update_clamps(tmp_path):
    mgr = TaskConfigManager(filepath=str(tmp_path / "task_config.json"))
    cfg = mgr.update_config({"frame_budget": 10 ** 9, "max_workers": 0, "summarize_batch_concurrency": 99})
    assert cfg["frame_budget"] == 200_000
    assert cfg["max_workers"] == 1
    assert cfg["summarize_batch_concurrency"] == 8
    # 未知键被忽略
    mgr.update_config({"bogus": 1})
    assert "bogus" not in json.loads((tmp_path / "task_config.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _adapt_frame_budget 已在 test_video_reader_time_and_budget 覆盖；
# 这里补 _rebuild_video_grids 三种 mode 与 _summarize_text 升格阶梯。
# ---------------------------------------------------------------------------

class _FakeVideoReader:
    calls = []

    def __init__(self, **kw):
        self.kw = dict(kw)
        _FakeVideoReader.calls.append(self.kw)

    def run(self, reuse_frames=False):
        self.kw["reuse_frames"] = reuse_frames
        return ["grid.jpg"]


@pytest.fixture
def rebuilder(tmp_path, monkeypatch):
    (tmp_path / "video.mp4").write_bytes(b"x")
    _FakeVideoReader.calls = []
    monkeypatch.setattr(note_mod, "VideoReader", _FakeVideoReader)
    monkeypatch.setattr(note_mod, "_frame_budget", lambda: 2400)

    def _make(last_grid):
        g = NoteGenerator.__new__(NoteGenerator)
        g.video_path = str(tmp_path / "video.mp4")
        g._last_frame_interval = 6
        g._last_grid_size = list(last_grid)
        g._task_scratch_dirs = lambda tid: (str(tmp_path / "f"), str(tmp_path / "g"))
        return g

    return _make


def test_rebuild_grid2_uses_2x2_same_interval_and_reuses_frames(rebuilder):
    g = rebuilder([1, 1])
    urls = g._rebuild_video_grids("t", mode="grid2")
    assert urls == ["grid.jpg"]
    call = _FakeVideoReader.calls[-1]
    assert call["grid_size"] == (2, 2)
    assert call["frame_interval"] == 6
    assert call["reuse_frames"] is True


def test_rebuild_grid3_uses_3x3_same_interval(rebuilder):
    g = rebuilder([2, 2])
    g._rebuild_video_grids("t", mode="grid3")
    call = _FakeVideoReader.calls[-1]
    assert call["grid_size"] == (3, 3)
    assert call["frame_interval"] == 6
    assert call["reuse_frames"] is True


def test_rebuild_thinned_triples_interval_with_3x3_floor(rebuilder):
    g = rebuilder([1, 1])
    g._rebuild_video_grids("t", mode="thinned")
    call = _FakeVideoReader.calls[-1]
    assert call["grid_size"] == (3, 3)
    assert call["frame_interval"] == 18
    assert call["reuse_frames"] is False


def test_rebuild_thinned_keeps_configured_grid_when_already_3x3(rebuilder):
    g = rebuilder([3, 3])
    g._rebuild_video_grids("t", mode="thinned")
    call = _FakeVideoReader.calls[-1]
    assert call["grid_size"] == (3, 3)
    assert call["frame_interval"] == 18


class _AlwaysFailingGPT:
    def __init__(self):
        self.sources = []

    def summarize(self, source):
        self.sources.append((list(source.video_img_urls or []), source.screenshot))
        raise RuntimeError("boom")


def _run_ladder(monkeypatch, tmp_path, last_grid, expected_modes):
    g = NoteGenerator.__new__(NoteGenerator)
    g._update_status = lambda *a, **k: None
    g._handle_exception = lambda *a, **k: None
    g._last_grid_size = list(last_grid)
    g._last_frame_interval = 6
    modes = []
    monkeypatch.setattr(note_mod, "_frame_budget", lambda: 2400)

    def fake_rebuild(task_id=None, mode="thinned"):
        modes.append(mode)
        return ["rebuilt.jpg"]

    monkeypatch.setattr(g, "_rebuild_video_grids", fake_rebuild)
    gpt = _AlwaysFailingGPT()
    transcript = SimpleNamespace(segments=[])
    audio_meta = SimpleNamespace(title="t", raw_info={"tags": []})
    with pytest.raises(Exception):
        g._summarize_text(
            audio_meta=audio_meta,
            transcript=transcript,
            gpt=gpt,
            markdown_cache_file=tmp_path / "m.md",
            link=False,
            screenshot=False,
            formats=["toc"],
            style="minimal",
            extras="",
            video_img_urls=["full.jpg"],
            task_id="t1",
        )
    assert modes == expected_modes
    # 每一级依次降请求：full 带原图、升格/减帧带重建图、最后纯文本
    assert gpt.sources[0] == (["full.jpg"], False)
    assert gpt.sources[-1] == ([], False)


def test_ladder_from_1x1_has_two_grid_up_levels(monkeypatch, tmp_path):
    _run_ladder(monkeypatch, tmp_path, [1, 1], ["grid2", "grid3", "thinned"])


def test_ladder_from_2x2_skips_grid2(monkeypatch, tmp_path):
    _run_ladder(monkeypatch, tmp_path, [2, 2], ["grid3", "thinned"])


def test_ladder_from_3x3_only_thins(monkeypatch, tmp_path):
    _run_ladder(monkeypatch, tmp_path, [3, 3], ["thinned"])


# ---------------------------------------------------------------------------
# summarize 分批并发：乱序完成按索引归位 / checkpoint 连续前缀
# ---------------------------------------------------------------------------

def _chunk(text):
    return SimpleNamespace(
        segments=[TranscriptSegment(start=0, end=1, text=text)],
        image_urls=[],
        meta=None,
    )


def _make_gpt(tmp_path, monkeypatch, concurrency, behaviors):
    """behaviors: {chunk文本: (sleep秒, 返回内容 或 Exception)}，按完成顺序乱序。"""
    # 打在类对象上（sys.modules["app"] 可能被前面的测试换成空壳桩，字符串路径会炸）
    monkeypatch.setattr(
        TaskConfigManager, "get",
        lambda self, key: concurrency if key == "summarize_batch_concurrency" else 2,
    )
    gpt = UniversalGPT(client=SimpleNamespace(), model="mock-model")
    gpt.checkpoint_dir = tmp_path

    fake_chunks = [_chunk(t) for t in behaviors]

    class _FakeChunker:
        def __init__(self, *a, **k):
            pass

        def chunk(self, segments, image_urls, **kwargs):
            return list(fake_chunks)

    import app.gpt.universal_gpt as ug_mod

    monkeypatch.setattr(ug_mod, "RequestChunker", _FakeChunker)

    def fake_create(messages):
        content = messages[0]["content"]
        m = re.search(r"chunk-(\d+)", content)
        key = f"chunk-{m.group(1)}"
        sleep_s, payload = behaviors[key]
        time.sleep(sleep_s)
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )

    gpt._chat_completion_create = fake_create
    merged = {}
    gpt._merge_partials = lambda partials, *a: "|".join(partials) if not merged.update(result="|".join(partials)) else "|".join(partials)
    return gpt, merged


def _source(tmp_path, checkpoint_key=None):
    return SimpleNamespace(
        title="t",
        tags=[],
        segment=[],
        screenshot=False,
        link=False,
        _format=["toc"],
        style="minimal",
        extras="",
        video_img_urls=[],
        checkpoint_key=checkpoint_key,
    )


def test_summarize_parallel_out_of_order_preserves_batch_order(tmp_path, monkeypatch):
    # chunk-0 最慢、chunk-2 最快：完成顺序 2,1,0，合并必须仍是 0|1|2
    behaviors = {
        "chunk-0": (0.4, "part-0"),
        "chunk-1": (0.1, "part-1"),
        "chunk-2": (0.0, "part-2"),
    }
    gpt, merged = _make_gpt(tmp_path, monkeypatch, concurrency=3, behaviors=behaviors)
    out = gpt.summarize(_source(tmp_path))
    assert out == "part-0|part-1|part-2"


def test_summarize_parallel_batch_failure_propagates(tmp_path, monkeypatch):
    behaviors = {
        "chunk-0": (0.0, "part-0"),
        "chunk-1": (0.0, RuntimeError("upstream boom")),
        "chunk-2": (1.5, "part-2"),
    }
    gpt, merged = _make_gpt(tmp_path, monkeypatch, concurrency=2, behaviors=behaviors)
    with pytest.raises(RuntimeError, match="upstream boom"):
        gpt.summarize(_source(tmp_path))


def test_summarize_serial_failure_saves_contiguous_checkpoint(tmp_path, monkeypatch):
    # 串行（并发=1）：chunk0、chunk1 成功，chunk2 失败 → checkpoint 落 [p0, p1]
    behaviors = {
        "chunk-0": (0.0, "part-0"),
        "chunk-1": (0.0, "part-1"),
        "chunk-2": (0.0, RuntimeError("boom-2")),
    }
    gpt, _ = _make_gpt(tmp_path, monkeypatch, concurrency=1, behaviors=behaviors)
    with pytest.raises(RuntimeError, match="boom-2"):
        gpt.summarize(_source(tmp_path, checkpoint_key="task-x"))
    cp = tmp_path / "task-x.gpt.checkpoint.json"
    payload = json.loads(cp.read_text(encoding="utf-8"))
    assert payload["partials"] == ["part-0", "part-1"]


# ---------------------------------------------------------------------------
# 供应商级思考强度
# ---------------------------------------------------------------------------

class _RecordingCompletions:
    def __init__(self, fail_first_with=None):
        self.calls = []
        self.fail_first_with = fail_first_with

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


class _StreamCompletions:
    """流式版 fake：_stream_create 会迭代返回值。"""

    def __init__(self, fail_first_with=None):
        self.calls = []
        self.fail_first_with = fail_first_with

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with
        return [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok", reasoning_content=None, reasoning=None),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content=None, reasoning=None),
                        finish_reason="stop",
                    )
                ]
            ),
        ]


def test_call_llm_attaches_reasoning_effort_and_drops_on_rejection(monkeypatch):
    from app.services import chat_service

    gpt = SimpleNamespace(model="m", thinking_effort="low")
    completions = _RecordingCompletions(
        fail_first_with=Exception("Unknown parameter: 'reasoning_effort' is not supported")
    )
    gpt.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    resp = chat_service._call_llm(gpt, [{"role": "user", "content": "hi"}], use_tools=False)
    assert resp.choices[0].message.content == "ok"
    assert completions.calls[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in completions.calls[1]


def test_call_llm_no_effort_when_unset(monkeypatch):
    from app.services import chat_service

    gpt = SimpleNamespace(model="m", thinking_effort=None)
    completions = _RecordingCompletions()
    gpt.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    chat_service._call_llm(gpt, [{"role": "user", "content": "hi"}], use_tools=False)
    assert "reasoning_effort" not in completions.calls[0]


def test_universal_gpt_stream_create_sends_effort():
    completions = _StreamCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    gpt = UniversalGPT(client=client, model="m", thinking_effort="HIGH")
    assert gpt.thinking_effort == "high"  # 归一化
    gpt._do_create([{"role": "user", "content": "x"}])
    assert completions.calls[0]["reasoning_effort"] == "high"

    gpt2 = UniversalGPT(client=client, model="m")
    gpt2._do_create([{"role": "user", "content": "x"}])
    assert "reasoning_effort" not in completions.calls[1]


def test_universal_gpt_drops_effort_on_rejection():
    completions = _StreamCompletions(
        fail_first_with=Exception("reasoning.effort unsupported value: xhigh")
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    gpt = UniversalGPT(client=client, model="m", thinking_effort="xhigh")
    gpt._do_create([{"role": "user", "content": "x"}])
    assert completions.calls[0].get("reasoning_effort") == "xhigh"
    assert "reasoning_effort" not in completions.calls[1]


def test_is_reasoning_unsupported_error_scope():
    yes = Exception("Unknown parameter: 'reasoning_effort'")
    also_yes = Exception("invalid value for reasoning effort")
    no = Exception("Your reasoning budget was exceeded")
    assert UniversalGPT._is_reasoning_unsupported_error(yes)
    assert UniversalGPT._is_reasoning_unsupported_error(also_yes)
    assert not UniversalGPT._is_reasoning_unsupported_error(no)


def test_responses_compat_maps_reasoning_effort():
    captured = {}

    class _FakeResponses:
        def create(self, **body):
            captured.update(body)
            return SimpleNamespace(output=[], output_text="ok")

    _Completions(SimpleNamespace(responses=_FakeResponses())).create(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="max",
    )
    assert captured["reasoning"] == {"effort": "max"}


def test_gpt_factory_passes_thinking_effort():
    cfg = ModelConfig(
        name="p", provider="custom", api_key="sk-test", base_url="https://example.invalid/v1",
        model_name="m", thinking_effort="low",
    )
    gpt = GPTFactory().from_config(cfg)
    assert getattr(gpt, "thinking_effort", None) == "low"

    cfg2 = ModelConfig(
        name="p", provider="custom", api_key="sk-test", base_url="https://example.invalid/v1",
        model_name="m",
    )
    gpt2 = GPTFactory().from_config(cfg2)
    assert getattr(gpt2, "thinking_effort", None) is None


# ---------------------------------------------------------------------------
# thinking_effort 归属：模型级（2026-10-11 从供应商级迁来）
# ---------------------------------------------------------------------------

def test_get_gpt_effort_comes_from_model_row(monkeypatch):
    """_get_gpt 只读 models 行的思考强度，供应商 dict 上的遗留值不许再回读。"""
    captured = {}

    class _FakeGPT:
        pass

    class _FakeFactory:
        def from_config(self, cfg):
            captured["cfg"] = cfg
            return _FakeGPT()

    monkeypatch.setattr(
        note_mod, "ProviderService",
        SimpleNamespace(get_provider_by_id=lambda pid: {
            "id": pid, "api_key": "sk", "base_url": "https://x/v1", "type": "custom",
            "name": "p", "api_format": "chat", "extra_headers": "{}",
            "thinking_effort": "SHOULD_NOT_BE_READ",
        }),
    )
    monkeypatch.setattr(note_mod, "GPTFactory", _FakeFactory)
    rows = {"model-x": {"id": 1, "model_name": "model-x", "thinking_effort": "xhigh"}}
    monkeypatch.setattr(note_mod, "get_model_by_provider_and_name", lambda pid, name: rows.get(name))

    g = NoteGenerator.__new__(NoteGenerator)
    g._get_gpt("model-x", "pid-1")
    assert captured["cfg"].thinking_effort == "xhigh"

    # 模型行缺失（如模型已删）：按未设置处理，绝不落回供应商遗留值
    monkeypatch.setattr(note_mod, "get_model_by_provider_and_name", lambda pid, name: None)
    g._get_gpt("ghost", "pid-1")
    assert captured["cfg"].thinking_effort is None


def test_init_db_migrates_provider_effort_to_models(tmp_path):
    """一次性搬迁：供应商 effort 复制到其全部模型行（模型已有值不覆盖），随后清空供应商列。"""
    from sqlalchemy import create_engine, text

    from app.db import init_db as init_db_mod

    eng = create_engine(f"sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE providers (id TEXT PRIMARY KEY, thinking_effort VARCHAR DEFAULT '')"
        ))
        conn.execute(text(
            "CREATE TABLE models (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER, "
            "model_name TEXT, thinking_effort VARCHAR DEFAULT '')"
        ))
        conn.execute(text(
            "INSERT INTO providers (id, thinking_effort) VALUES ('p-open', 'low'), ('p-none', ''), ('123', 'high')"
        ))
        conn.execute(text(
            "INSERT INTO models (provider_id, model_name, thinking_effort) VALUES "
            "('p-open', 'm1', ''), ('p-open', 'm2', 'max'), ('p-none', 'm3', ''), (123, 'm4', '')"
        ))

    init_db_mod._migrate_provider_effort_to_models(eng)
    # 幂等：再跑一次不得改写
    init_db_mod._migrate_provider_effort_to_models(eng)

    with eng.connect() as conn:
        provider_vals = conn.execute(
            text("SELECT id, thinking_effort FROM providers ORDER BY id")
        ).fetchall()
        model_vals = conn.execute(
            text("SELECT model_name, thinking_effort FROM models ORDER BY id")
        ).fetchall()
    assert [tuple(r) for r in provider_vals] == [("123", ""), ("p-none", ""), ("p-open", "")]
    # m2 已有值不覆盖；m4 靠字符串/数字双形态匹配拿到 'high'
    assert [tuple(r) for r in model_vals] == [
        ("m1", "low"), ("m2", "max"), ("m3", ""), ("m4", "high"),
    ]


# ---------------------------------------------------------------------------
# 执行器热换池 / VideoReader 帧复用与并行
# ---------------------------------------------------------------------------

def test_set_max_workers_hot_swap():
    ex = ConcurrentTaskExecutor(max_workers=2)
    try:
        ex.set_max_workers(3)
        assert ex._max_workers == 3
        state_lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def work():
            with state_lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.15)
            with state_lock:
                state["active"] -= 1

        threads = [threading.Thread(target=lambda: ex.run(work)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert state["peak"] == 3
    finally:
        ex.shutdown(wait=False)


def _make_frame(path, color):
    # 顶层捕获的真 PIL（test_video_reader_dedupe 收集期会把 sys.modules["PIL.Image"]
    # 永久换成假模块，运行期再 import 拿到的是假货）
    PILImage.new("RGB", (8, 6), color).save(path, quality=80)


def test_video_reader_run_reuses_cached_frames_and_parallel(tmp_path):
    """run(reuse_frames=True)：帧已缓存时跳过重新抽帧（不需要 ffmpeg），
    拼图+编码走并行路径且保序。"""
    frame_dir = tmp_path / "frames"
    grid_dir = tmp_path / "grids"
    frame_dir.mkdir()
    # 9 帧合法时间戳文件名 → 一张 3×3 拼图
    for sec in range(9):
        _make_frame(frame_dir / f"frame_00_{sec:02d}.jpg", (sec * 20, 0, 0))

    reader = VideoReader(
        video_path="dummy.mp4",
        grid_size=(3, 3),
        frame_dir=str(frame_dir),
        grid_dir=str(grid_dir),
        save_quality=80,
    )
    urls = reader.run(reuse_frames=True)
    assert len(urls) == 1
    assert urls[0].startswith("data:image/jpeg;base64,")
    head = json.loads(json.dumps(urls[0][:40]))
    assert "base64" in head
    # 解码校验：确实是有效 JPEG
    raw = base64.b64decode(urls[0].split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"
    # 复用路径不应触碰 frame_dir（时间戳文件仍在）
    assert len([f for f in os.listdir(frame_dir) if f.startswith("frame_")]) == 9


def test_encode_images_to_base64_parallel_preserves_order(tmp_path):
    paths = []
    for i in range(6):
        p = tmp_path / f"f{i}.jpg"
        _make_frame(p, (i * 30, 0, 0))
        paths.append(str(p))
    reader = VideoReader(video_path="dummy", frame_dir=str(tmp_path / "f2"), grid_dir=str(tmp_path / "g2"))
    urls = reader.encode_images_to_base64(paths)
    assert urls == reader.encode_images_to_base64(list(reversed(paths)))[::-1]
    assert all(u.startswith("data:image/jpeg;base64,") for u in urls)

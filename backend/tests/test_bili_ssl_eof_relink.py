"""末次换链路回归测试（2026-10-05「交通法」笔记实录）。

SSL EOF / 连接重置这类 CDN 抖动若连续打满瞬态重试，残留分片的 Range 续传
请求会钉死在同一个坏的 CDN 节点上。约定：最后一次尝试前必须清分片 +
关续传从头下，让新连接有机会被调度到健康节点。
"""
import importlib.util
import pathlib
import sys
import types
from abc import ABC

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "downloaders" / "bilibili_downloader.py"


def _stub(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


class _DownloadError(Exception):
    pass


class _ScriptFailingYoutubeDL:
    """按脚本失败：script[i] 为异常则抛，否则返回成功 dict。记录每次的 opts。"""
    script = []
    seen_opts = []

    def __init__(self, opts):
        type(self).seen_opts.append(dict(opts))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, _url, download=True):
        action = type(self).script.pop(0)
        if isinstance(action, Exception):
            raise action
        return {"id": "v", "title": "t"}


class _DownloaderBase(ABC):
    pass


def _load(monkeypatch, script):
    _stub(monkeypatch, "app")
    _stub(monkeypatch, "app.downloaders")
    _stub(monkeypatch, "app.downloaders.base",
           Downloader=_DownloaderBase, DownloadQuality=str, QUALITY_MAP={},
           YDL_RETRY_OPTS={"retries": 3})
    _stub(monkeypatch, "app.downloaders.bilibili_dm_patch",
           apply_bilibili_dm_img_patch=lambda: None)
    _stub(monkeypatch, "app.downloaders.bilibili_subtitle",
           BilibiliSubtitleFetcher=object)
    _stub(monkeypatch, "app.models")
    _stub(monkeypatch, "app.models.notes_model", AudioDownloadResult=object)
    _stub(monkeypatch, "app.models.transcriber_model",
           TranscriptResult=object, TranscriptSegment=object)
    _stub(monkeypatch, "app.services")
    _stub(monkeypatch, "app.services.cookie_manager",
           CookieConfigManager=type("CookieConfigManager", (), {}))
    _stub(monkeypatch, "app.utils")
    _stub(monkeypatch, "app.utils.url_parser", extract_video_id=lambda *a, **k: "v")
    _stub(monkeypatch, "app.utils.logger",
           get_logger=lambda *a, **k: types.SimpleNamespace(
               info=lambda *a, **k: None, warning=lambda *a, **k: None,
               error=lambda *a, **k: None))
    _stub(monkeypatch, "app.utils.path_helper", get_data_dir=lambda: "/tmp")
    yt = types.ModuleType("yt_dlp")
    yt.YoutubeDL = _ScriptFailingYoutubeDL
    yt_utils = types.ModuleType("yt_dlp.utils")
    yt_utils.DownloadError = _DownloadError
    monkeypatch.setitem(sys.modules, "yt_dlp", yt)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", yt_utils)
    _ScriptFailingYoutubeDL.script = list(script)
    _ScriptFailingYoutubeDL.seen_opts = []
    spec = importlib.util.spec_from_file_location("bili_dl_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ssl_eof_last_attempt_clears_partials_and_disables_continue(tmp_path, monkeypatch):
    module = _load(monkeypatch, [
        _DownloadError("[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"),
        _DownloadError("[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"),
        {"ok": True},
    ])
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    part = tmp_path / "BVtest.part"
    part.write_bytes(b"junk")
    module._ydl_extract_download({"continuedl": True}, "https://www.bilibili.com/video/BVtest",
                                  str(tmp_path), "BVtest")
    assert len(_ScriptFailingYoutubeDL.seen_opts) == 3
    # 前两次保留续传，第三次（末次）必须从头下
    assert _ScriptFailingYoutubeDL.seen_opts[0]["continuedl"] is True
    assert _ScriptFailingYoutubeDL.seen_opts[1]["continuedl"] is True
    assert _ScriptFailingYoutubeDL.seen_opts[2]["continuedl"] is False
    assert not part.exists()


def test_416_clears_partials_on_first_retry(tmp_path, monkeypatch):
    module = _load(monkeypatch, [
        _DownloadError("HTTP Error 416: Requested Range Not Satisfiable"),
        {"ok": True},
    ])
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    part = tmp_path / "BVtest.part"
    part.write_bytes(b"junk")
    module._ydl_extract_download({"continuedl": True}, "https://www.bilibili.com/video/BVtest",
                                  str(tmp_path), "BVtest")
    assert _ScriptFailingYoutubeDL.seen_opts[1]["continuedl"] is False
    assert not part.exists()


def test_non_transient_error_raises_immediately(tmp_path, monkeypatch):
    module = _load(monkeypatch, [_DownloadError("HTTP Error 412: Precondition Failed")])
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    with pytest.raises(_DownloadError):
        module._ydl_extract_download({}, "https://www.bilibili.com/video/BVtest",
                                     str(tmp_path), "BVtest")
    assert len(_ScriptFailingYoutubeDL.seen_opts) == 1

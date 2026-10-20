"""运行时资源隔离：下载器每任务独立、Bcut 转写器不再共享上传会话。"""
from app.services.constant import SUPPORT_PLATFORM_MAP
from app.services.note import NoteGenerator
from app.transcriber import transcriber_provider as tp


def test_platform_map_holds_classes():
    assert SUPPORT_PLATFORM_MAP
    assert all(isinstance(value, type) for value in SUPPORT_PLATFORM_MAP.values())


def test_downloader_instances_are_per_run():
    gen = object.__new__(NoteGenerator)
    first = gen._get_downloader("youtube")
    second = gen._get_downloader("youtube")

    assert first is not second
    first.cancel_check = lambda: True
    assert second.cancel_check is None, "一个任务的取消谓词不能泄漏到另一个任务"


def test_bcut_transcriber_created_per_call(monkeypatch):
    created = []

    class FakeBcut:
        def __init__(self):
            created.append(self)

    monkeypatch.setattr(tp, "BcutTranscriber", FakeBcut)

    first = tp.get_bcut_transcriber()
    second = tp.get_bcut_transcriber()

    assert first is not second
    assert len(created) == 2


def test_whisper_transcriber_remains_cached(monkeypatch):
    class FakeWhisper:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(tp, "WhisperTranscriber", FakeWhisper)
    monkeypatch.setitem(tp._transcribers, tp.TranscriberType.FAST_WHISPER, None)
    monkeypatch.setitem(tp._transcriber_configs, tp.TranscriberType.FAST_WHISPER, None)

    first = tp.get_whisper_transcriber("tiny", "cpu")
    second = tp.get_whisper_transcriber("tiny", "cpu")

    assert first is second, "whisper 模型加载昂贵，必须继续按配置缓存"

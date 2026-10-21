from app.routers import note as note_router


def test_youtube_cover_uses_configured_proxy(monkeypatch):
    monkeypatch.setattr(
        note_router.ProxyConfigManager,
        "get_proxy_url",
        lambda self: "http://127.0.0.1:7892",
    )
    monkeypatch.setattr(note_router, "proxy_reachable", lambda url: True)

    assert (
        note_router._cover_proxy_url("https://i.ytimg.com/vi/abc/hqdefault.jpg")
        == "http://127.0.0.1:7892"
    )


def test_youtube_cover_falls_back_to_direct_when_proxy_dead(monkeypatch):
    monkeypatch.setattr(
        note_router.ProxyConfigManager,
        "get_proxy_url",
        lambda self: "http://127.0.0.1:7892",
    )
    monkeypatch.setattr(note_router, "proxy_reachable", lambda url: False)

    assert note_router._cover_proxy_url("https://i.ytimg.com/vi/abc/hqdefault.jpg") is None


def test_bilibili_cover_does_not_use_proxy(monkeypatch):
    calls = []

    def _proxy_url(_self):
        calls.append(1)
        return "http://127.0.0.1:7892"

    monkeypatch.setattr(note_router.ProxyConfigManager, "get_proxy_url", _proxy_url)

    assert note_router._cover_proxy_url("https://i0.hdslb.com/bfs/archive/x.jpg") is None
    assert calls == []

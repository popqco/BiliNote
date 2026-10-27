from app.downloaders import wechat_common as wc
from app.services import task_state
from app.utils.url_parser import extract_video_id


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_wechat_url_ids():
    assert extract_video_id("https://weixin.qq.com/sph/AS0dQxG4rv", "wechat_channels") == "AS0dQxG4rv"
    assert extract_video_id("https://channels.weixin.qq.com/mobile/sf/abc123", "wechat_channels") == "abc123"
    assert extract_video_id("https://mp.weixin.qq.com/s/8MBXs7XAGAXvEJukfHqn4w", "wechat_article") == "8MBXs7XAGAXvEJukfHqn4w"
    assert extract_video_id("https://mp.weixin.qq.com/s?__biz=x&sn=abc123", "wechat_article") == "abc123"


def test_parse_article_html_extracts_title_and_video():
    body = """
    <html><head><meta property="og:image" content="http://mmbiz.qpic.cn/cover.jpg"></head>
    <body>
    <script>
      var msg_title = '测试文章标题';
      nick_name: '测试公众号';
      mp_video_trans_info: [
        {
          format_id: '10002',
          filesize: '123456',
          duration_ms: '65000',
          url: 'http://mpvideo.qpic.cn/a.f10002.mp4?dis_k=x\\x26amp;dis_t=123',
        },
      ];
    </script>
    <iframe data-mpvid="wxv_123456789012345678"></iframe>
    </body></html>
    """
    meta = wc.parse_article_html(body)
    assert meta["title"] == "测试文章标题"
    assert meta["uploader"] == "测试公众号"
    assert meta["cover_url"].startswith("https://")
    assert meta["duration"] == 65.0
    assert meta["video_id"] == "wxv_123456789012345678"
    assert meta["videos"][0]["url"] == "http://mpvideo.qpic.cn/a.f10002.mp4?dis_k=x&dis_t=123"


def test_channels_public_meta(monkeypatch):
    class FakeSession:
        def post(self, url, **kwargs):
            return FakeResponse(201, {
                "errCode": 0,
                "data": {
                    "authorInfo": {"nickname": "测试作者"},
                    "feedInfo": {
                        "description": "测试视频标题 #话题",
                        "coverUrl": "https://finder.video.qq.com/cover.jpg",
                    },
                },
            })

        def close(self):
            pass

    monkeypatch.setattr(wc.requests, "Session", lambda: FakeSession())
    meta = wc.fetch_channels_public_meta("https://weixin.qq.com/sph/AS0dQxG4rv")
    assert meta["title"] == "测试视频标题 #话题"
    assert meta["uploader"] == "测试作者"
    assert meta["video_id"] == "AS0dQxG4rv"
    assert meta["cover_url"].endswith("cover.jpg")


def test_resolve_channels_with_yuanbao(monkeypatch):
    calls = []

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if "get_parse_result" in url:
                return FakeResponse(200, {
                    "code": 0,
                    "data": {"playable_url": "https://channels.weixin.qq.com/web/pages/feed?eid=export%2Fabc&token=tok123"},
                })
            return FakeResponse(201, {
                "errCode": 0,
                "data": {
                    "authorInfo": {"nickname": "作者"},
                    "feedInfo": {
                        "description": "标题",
                        "coverUrl": "https://cover",
                        "h264VideoInfo": {"videoUrl": "https://cdn/video.mp4?decode_key=42"},
                        "decodeKey": "42",
                        "videoPlayLen": 12,
                    },
                },
            })

        def close(self):
            pass

    monkeypatch.setattr(wc.requests, "Session", lambda: FakeSession())
    meta = wc.resolve_channels_with_yuanbao("https://weixin.qq.com/sph/AS0dQxG4rv", "hy_token=x")
    assert meta["video_url"] == "https://cdn/video.mp4?decode_key=42"
    assert meta["decode_key"] == "42"
    assert meta["duration"] == 12
    assert len(calls) == 2


def test_isaac64_stream_roundtrip():
    original = bytes(range(256)) * 512  # 131072 bytes
    data = bytearray(original)
    wc.decrypt_channels_data(data, 123456789)
    assert bytes(data) != original
    wc.decrypt_channels_data(data, 123456789)
    assert bytes(data) == original


def test_merge_status_meta_does_not_clear_existing_meta(tmp_path):
    task_id = "t1"
    task_state.write_status(
        task_id,
        "SUCCESS",
        extra={"audio_meta": {"title": "已有标题", "cover_url": "https://cover", "duration": 10}},
        root=tmp_path,
    )
    task_state.merge_status_meta(
        task_id,
        {"audio_meta": {"title": None, "cover_url": "", "duration": 0, "video_id": "BV1xx"}},
        root=tmp_path,
    )
    data = task_state.read_status(task_id, root=tmp_path)
    assert data["audio_meta"]["title"] == "已有标题"
    assert data["audio_meta"]["cover_url"] == "https://cover"
    assert data["audio_meta"]["duration"] == 10
    assert data["audio_meta"]["video_id"] == "BV1xx"

import os
import tempfile
import time
from abc import ABC
from typing import Union, Optional, List

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality, YDL_RETRY_OPTS
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.services.cookie_manager import CookieConfigManager, iter_cookie_pairs
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _apply_proxy(ydl_opts: dict) -> dict:
    """YouTube 在国内需要代理。配置了全局代理就塞进 yt-dlp opts。"""
    proxy = ProxyConfigManager().get_proxy_url()
    if proxy:
        ydl_opts['proxy'] = proxy
        logger.info(f"yt-dlp 走代理: {proxy}")
    return ydl_opts


# 年龄限制视频的 PoToken 生成脚本（bgutil-ytdlp-pot-provider 的 script 模式）
DEFAULT_BGUTIL_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".bilinote-bgutil", "server", "build", "generate_once.js")
BGUTIL_SCRIPT_ENV = "BILINOTE_BGUTIL_SCRIPT"


def apply_youtube_extras(ydl_opts: dict) -> None:
    """给 yt-dlp 补上 YouTube 现在必需的两件套（缺了会「格式全部丢弃」或「年龄门过不去」）：

    1) JS 运行时（node）+ yt-dlp-ejs 求解器：YouTube 的 n-sig 挑战需要，否则所有
       媒体格式会被丢弃（报 Requested format is not available）；
    2) bgutil PoToken provider（脚本模式）：年龄限制内容（"Sorry, this content is
       age-restricted"）要求请求带 PoToken，yt-dlp 自身不会生成。

    两者都以本机存在为前提，缺失时静默降级（普通视频不受影响）。
    """
    # Python API 要求 {runtime: {config}} 的 dict 形式（CLI 的 --js-runtimes 会转成这样）
    runtimes = ydl_opts.setdefault('js_runtimes', {})
    for r in ('deno', 'node'):
        runtimes.setdefault(r, {})
    script = os.environ.get(BGUTIL_SCRIPT_ENV) or DEFAULT_BGUTIL_SCRIPT
    if script and os.path.isfile(script):
        ea = ydl_opts.setdefault('extractor_args', {})
        ea.setdefault('youtubepot-bgutilscript', {})['script_path'] = [script]
    else:
        logger.info("未找到 PoToken 生成脚本（%s），年龄限制视频可能无法下载", script)



def resolve_youtube_cookie_pairs():
    """返回 (pairs, source)；实现见 services/youtube_profile_cookies。

    优先「专用浏览器配置」（免维护、浏览器自动轮换），其次手工粘贴的 Cookie。
    """
    from app.services.youtube_profile_cookies import resolve_youtube_cookie_pairs as _impl
    return _impl()


def _netscape_line(name: str, value: str, domain: str = ".youtube.com") -> str:
    """Netscape 行必须带**未来的**过期时间与 secure 标志。

    yt-dlp 以 `MozillaCookieJar.load()` 默认参数读 cookiefile：过期/会话 cookie
    （expiry=0）会被直接丢弃——曾出现 32 条只加载进 2 条、等于没登录的坑。
    domain 必须逐条保留：HSID/SSID 在 .google.com 与 .youtube.com 下的值不同，
    乱改域会让 YouTube 会话签名不匹配（明明带 cookie 却报 "Sign in to confirm your age"）。
    """
    expires = int(time.time()) + 365 * 24 * 3600
    include_sub = "TRUE" if str(domain).startswith(".") else "FALSE"
    return f"{domain}\t{include_sub}\t/\tTRUE\t{expires}\t{name}\t{value}\n"


def write_youtube_cookiefile() -> Optional[str]:
    """把 YouTube Cookie 写成 Netscape 格式临时文件，供 yt-dlp cookiefile 使用。

    数据来源（按优先级）：
    1) 「专用浏览器配置」（~/.bilinote-youtube-profile）里登录后的 cookie —— 免维护，
       浏览器每次运行会自动轮换 __Secure-1PSIDTS 等短周期令牌；
    2) 用户在「设置 → 下载配置 → YouTube」手工粘贴的 cookies.txt 全文或 Cookie 串。

    年龄限制视频（"Sign in to confirm your age"）必须带登录 Cookie 才能下载。
    都没有配置时返回 None——与之前行为一致（匿名下载）。
    返回的临时文件由调用方在下载结束后删除（yt-dlp 退出时会回写该文件，属正常）。
    """
    from app.services.youtube_profile_cookies import resolve_youtube_cookies
    cookies, source = resolve_youtube_cookies()
    if source == "config":
        raw = str(CookieConfigManager().get("youtube") or "").strip()
        if "# Netscape" in raw or "\t" in raw:
            content = raw if raw.endswith("\n") else raw + "\n"
        else:
            lines = ["# Netscape HTTP Cookie File\n"]
            for c in cookies:
                lines.append(_netscape_line(c["name"], c["value"], c["domain"]))
            content = "".join(lines)
    else:
        if not cookies:
            return None
        lines = ["# Netscape HTTP Cookie File\n"]
        for c in cookies:
            lines.append(_netscape_line(c["name"], c["value"], c["domain"]))
        content = "".join(lines)
    entries = sum(1 for _ in iter_cookie_pairs(content))
    if not entries:
        logger.warning("YouTube Cookie 无法解析出任何条目，忽略并匿名下载")
        return None
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
    tmp.write(content)
    tmp.close()
    logger.info("已生成 YouTube Netscape Cookie 文件: %s (来源: %s, 条目: %d)", tmp.name, source, entries)
    return tmp.name


def apply_youtube_cookie(ydl_opts: dict) -> Optional[str]:
    """按需把 YouTube cookiefile 注入 ydl_opts，返回临时文件路径（无 Cookie 时为 None）。"""
    try:
        path = write_youtube_cookiefile()
    except Exception as e:
        logger.warning(f"生成 YouTube Cookie 文件失败，匿名下载: {e}")
        return None
    if path:
        ydl_opts['cookiefile'] = path
    return path


class YoutubeDownloader(Downloader, ABC):
    def __init__(self):

        super().__init__()

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            **YDL_RETRY_OPTS,
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
        }

        if skip_download:
            ydl_opts['skip_download'] = True
            # 只取元信息时并不需要媒体流。yt-dlp 版本落后于 YouTube player 时，
            # nsig 解析失败会导致所有音视频格式被丢弃，此时格式选择会抛
            # "Requested format is not available"，把一个已经拿到字幕的任务带崩。
            ydl_opts['ignore_no_formats_error'] = True

        _apply_proxy(ydl_opts)
        apply_youtube_extras(ydl_opts)
        cookiefile = apply_youtube_cookie(ydl_opts)
        # 用户点停止后，正在跑的下载在下一个进度回调处中断（见 Downloader._ydl_opts_with_cancel）
        ydl_opts = self._ydl_opts_with_cancel(ydl_opts)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=not skip_download)
                video_id = info.get("id")
                title = info.get("title")
                duration = info.get("duration", 0)
                cover_url = info.get("thumbnail")
                # skip_download 时 yt-dlp 返回 ext=None，默认值不会生效，避免拼出 "xxx.None"
                ext = info.get("ext") or "m4a"
                audio_path = os.path.join(output_dir, f"{video_id}.{ext}")
        finally:
            if cookiefile and os.path.exists(cookiefile):
                os.remove(cookiefile)

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="youtube",
            video_id=video_id,
            raw_info={'tags': info.get('tags')},
            video_path=None,
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """
        if output_dir is None:
            output_dir = get_data_dir()
        video_id = extract_video_id(video_url, "youtube")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            **YDL_RETRY_OPTS,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
        }

        _apply_proxy(ydl_opts)
        apply_youtube_extras(ydl_opts)
        cookiefile = apply_youtube_cookie(ydl_opts)
        # 用户点停止后，正在跑的下载在下一个进度回调处中断（见 Downloader._ydl_opts_with_cancel）
        ydl_opts = self._ydl_opts_with_cancel(ydl_opts)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                video_id = info.get("id")
                video_path = os.path.join(output_dir, f"{video_id}.mp4")
        finally:
            if cookiefile and os.path.exists(cookiefile):
                os.remove(cookiefile)

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        通过 YouTube InnerTube API 直接获取字幕（优先人工字幕，其次自动生成）。
        比 yt_dlp 方式更轻量，无需写临时文件到磁盘。

        :param video_url: 视频链接
        :param output_dir: 未使用（保留接口兼容）
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'zh-TW', 'en', 'en-US', 'ja']

        video_id = extract_video_id(video_url, "youtube")
        fetcher = YouTubeSubtitleFetcher()
        print(
            f"尝试获取字幕，video_id={video_id}, langs={langs}"
        )
        return fetcher.fetch_subtitles(video_id, langs)

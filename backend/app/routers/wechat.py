"""微信视频号：腾讯元宝扫码登录（获取视频号解析所需登录态）。

视频号没有匿名播放地址；开源方案都依赖元宝登录态。桌面端点击「扫码登录」
后由后端拉起一个真实 Edge 窗口，用户在窗口里用微信扫码；后端只轮询
cookie（不读取页面内容），拿到 hy_token 后保存到 config/downloader.json，
随后解析分享链接即可。

安全边界：登录窗口只允许在 Worker 本机（loopback）触发；Cookie 永不回传
给前端，只返回是否已配置。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R

logger = get_logger(__name__)
router = APIRouter()
_cookie_manager = CookieConfigManager()

_lock = threading.Lock()
_cancel = threading.Event()
_thread: Optional[threading.Thread] = None
_state = {
    "status": "idle",  # idle | running | success | failed | cancelled
    "message": "",
    "started_at": None,
    "finished_at": None,
}


def _set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _snapshot() -> dict:
    with _lock:
        data = dict(_state)
    data["has_cookie"] = bool(_cookie_manager.get("wechat_channels"))
    return data


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _cookie_header(cookies) -> str:
    parts = []
    for c in cookies or []:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _has_login_cookie(cookies) -> bool:
    names = {str(c.get("name") or "") for c in cookies or []}
    # 元宝登录后会有 hy_token / hy_user 等登录态 cookie；只看 hy_token 作主判据。
    return "hy_token" in names and any(
        str(c.get("name")) == "hy_token" and str(c.get("value") or "").strip()
        for c in cookies or []
    )


def _login_worker() -> None:
    _cancel.clear()
    _set_state(status="running", message="请在弹出的浏览器窗口里用微信扫码登录元宝", started_at=time.time(), finished_at=None)
    browser = None
    context = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    channel="msedge",
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception:
                browser = pw.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = context.new_page()
            page.goto("https://yuanbao.tencent.com/", wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 300
            saved = False
            while time.time() < deadline and not _cancel.is_set():
                cookies = context.cookies()
                if _has_login_cookie(cookies):
                    cookie_str = _cookie_header(cookies)
                    if cookie_str:
                        _cookie_manager.set("wechat_channels", cookie_str)
                        _set_state(
                            status="success",
                            message="元宝登录成功，视频号解析已可用",
                            finished_at=time.time(),
                        )
                        saved = True
                        break
                time.sleep(2)
            if not saved and not _cancel.is_set():
                _set_state(
                    status="failed",
                    message="等待登录超时（5 分钟）。请重新点击扫码登录，并在窗口内完成微信扫码。",
                    finished_at=time.time(),
                )
            elif _cancel.is_set():
                _set_state(status="cancelled", message="已取消扫码登录", finished_at=time.time())
    except Exception as e:
        logger.error(f"元宝扫码登录失败: {e}", exc_info=True)
        _set_state(status="failed", message=f"无法启动浏览器：{e}", finished_at=time.time())
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


@router.post("/wechat/yuanbao_login/start")
def start_yuanbao_login(request: Request):
    global _thread
    if not _is_loopback(request):
        return R.error(msg="扫码登录需要在 Worker 本机的 BiliNote 设置页操作（可手动粘贴元宝 Cookie）", code=403)
    with _lock:
        if _state.get("status") == "running" and _thread and _thread.is_alive():
            return R.success(data=_snapshot(), msg="登录窗口已在运行")
    _thread = threading.Thread(target=_login_worker, name="yuanbao-login", daemon=True)
    _thread.start()
    return R.success(data=_snapshot())


@router.get("/wechat/yuanbao_login/status")
def yuanbao_login_status():
    return R.success(data=_snapshot())


@router.post("/wechat/yuanbao_login/cancel")
def cancel_yuanbao_login(request: Request):
    if not _is_loopback(request):
        return R.error(msg="仅允许在 Worker 本机操作", code=403)
    _cancel.set()
    return R.success(data=_snapshot(), msg="已请求关闭登录窗口")


@router.post("/wechat/yuanbao_login/clear")
def clear_yuanbao_cookie(request: Request):
    if not _is_loopback(request):
        return R.error(msg="仅允许在 Worker 本机操作", code=403)
    _cookie_manager.delete("wechat_channels")
    _set_state(status="idle", message="已清除元宝登录态", started_at=None, finished_at=None)
    return R.success(data=_snapshot())


class ResolveRequest(BaseModel):
    url: str


@router.post("/wechat/channels/resolve")
def resolve_channels(data: ResolveRequest):
    """解析视频号分享链接（前端可用来预览标题/封面；不返回 CDN 地址）。"""
    from app.downloaders.wechat_common import (
        fetch_channels_public_meta,
        resolve_channels_with_yuanbao,
    )

    url = str(data.url or "").strip()
    try:
        meta = fetch_channels_public_meta(url)
    except Exception as e:
        return R.error(msg=f"视频号链接解析失败：{e}", code=400)
    cookie = _cookie_manager.get("wechat_channels")
    if cookie:
        try:
            rich = resolve_channels_with_yuanbao(url, cookie)
            meta.update({k: v for k, v in rich.items() if v not in (None, "", 0, [], {})})
        except Exception as e:
            return R.error(msg=f"元宝解析失败：{e}（请重新扫码登录元宝）", code=502, data={
                "title": meta.get("title"),
                "cover_url": meta.get("cover_url"),
                "video_id": meta.get("video_id"),
                "has_video": False,
            })
    return R.success(data={
        "title": meta.get("title"),
        "cover_url": meta.get("cover_url"),
        "uploader": meta.get("uploader"),
        "duration": meta.get("duration"),
        "video_id": meta.get("video_id"),
        "has_video": bool(meta.get("video_url")),
    })


@router.get("/wechat/article_probe")
def article_probe(request: Request, url: str):
    """诊断用：验证本机 Edge 能否打开公众号文章并捕获视频（不下载）。"""
    if not _is_loopback(request):
        return R.error(msg="仅允许在 Worker 本机诊断", code=403)
    from app.downloaders.wechat_common import (
        fetch_article_html,
        probe_article_video_via_browser,
    )

    try:
        meta = fetch_article_html(url)
        probe = probe_article_video_via_browser(meta.get("article_url") or url)
    except Exception as e:
        return R.error(msg=f"公众号视频探测失败：{e}", code=502)
    return R.success(data={
        "title": meta.get("title"),
        "video_id": meta.get("video_id"),
        **probe,
    })

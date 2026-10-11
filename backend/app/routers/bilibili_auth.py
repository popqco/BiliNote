"""B 站扫码登录 / Cookie 续期 / 登录态查询接口。

凭据本体永远只存在 config/downloader.json，接口只在扫码成功那一刻回显一次
完整 cookie（前端要填进输入框展示）；其余接口只返回状态与用户名。
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.utils.response import ResponseWrapper as R
from app.utils.logger import get_logger
from app.services import bilibili_auth
from app.services.cookie_manager import CookieConfigManager

logger = get_logger(__name__)

router = APIRouter()


class QrPollRequest(BaseModel):
    qrcode_key: str


@router.get("/bilibili/auth_status")
def auth_status():
    """前端「下载配置」页展示用：是否已登录、能否自动续期、上次续期时间、用户名。"""
    auth = CookieConfigManager().get_auth(bilibili_auth.PLATFORM)
    cookie = auth.get("cookie")
    has_refresh = bool(auth.get("refresh_token"))
    username = bilibili_auth.get_username(cookie) if cookie else None
    return R.success(data={
        "has_cookie": bool(cookie),
        "can_auto_refresh": has_refresh,
        "username": username,
        "updated_at": auth.get("updated_at"),
        "refreshed_at": auth.get("refreshed_at"),
    })


@router.post("/bilibili/qr/start")
def qr_start():
    """申请一个 B 站登录二维码（180 秒有效，前端用 qrcode.react 渲染）。"""
    try:
        return R.success(data=bilibili_auth.start_qr_login())
    except bilibili_auth.BilibiliAuthError as e:
        return R.error(msg=str(e))


@router.post("/bilibili/qr/poll")
def qr_poll(data: QrPollRequest):
    """轮询扫码结果（前端每 2 秒调一次）。

    status: waiting（未扫）/ scanned（已扫待确认）/ expired（已过期，需重新申请）
    / confirmed（登录成功，此时凭据已落盘并回显一次 cookie）。
    """
    try:
        return R.success(data=bilibili_auth.poll_qr_login(data.qrcode_key))
    except bilibili_auth.BilibiliAuthError as e:
        return R.error(msg=str(e))


@router.post("/bilibili/cookie/refresh")
def cookie_refresh():
    """手动触发一次 Cookie 检查+续期（「检查并续期」按钮）。"""
    result = bilibili_auth.ensure_cookie_fresh(force=True)
    if not result.get("ok"):
        return R.error(msg=result.get("reason") or "续期失败", data=result)
    msg = "Cookie 已续期" if result.get("refreshed") else "Cookie 仍然有效，无需续期"
    return R.success(data=result, msg=msg)

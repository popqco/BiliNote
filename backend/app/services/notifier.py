"""检查轮汇总通知：WxPusher（微信）与 SMTP（邮箱）双渠道。

设计要点（用户决策 Q1/Q9）：
- 两个渠道独立启用、独立失败——一个挂了不影响另一个；
- 只在每轮检查结束时发一条汇总（不逐条轰炸）；
- 所有发送结果都返回给调用方（设置页的「发送测试通知」直接展示每条渠道的结果）。
"""
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from typing import List, Tuple

import requests

from app.services.automation_config_manager import AutomationConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)


def send_wxpusher(app_token: str, uids: str, title: str, content_md: str) -> Tuple[bool, str]:
    uid_list = [u.strip() for u in str(uids or "").replace(";", ",").split(",") if u.strip()]
    if not app_token or not uid_list:
        return False, "WxPusher 未配置（需要 appToken 和至少一个 UID）"
    try:
        resp = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={
                "appToken": app_token,
                "content": content_md,
                "summary": title[:99],
                "contentType": 3,  # markdown
                "uids": uid_list,
            },
            timeout=15,
        )
        data = resp.json()
        ok = data.get("code") == 1000
        return ok, str(data.get("msg") or data)[:200]
    except Exception as e:
        return False, f"WxPusher 发送异常：{e}"


def send_email(cfg: dict, subject: str, body: str) -> Tuple[bool, str]:
    if not cfg.get("host") or not cfg.get("username") or not cfg.get("password") or not cfg.get("to"):
        return False, "SMTP 未配置（需要服务器/账号/授权码/收件人）"
    host = str(cfg["host"]).strip()
    port = int(cfg.get("port") or 465)
    username = cfg["username"]
    to_list = [t.strip() for t in str(cfg["to"]).replace(";", ",").split(",") if t.strip()]
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = username
        msg["To"] = cfg["to"]
        ctx = ssl.create_default_context()
        # 465 是隐式 SSL；587/25 是明文连接 + STARTTLS。原先只走 SMTP_SSL，
        # 于是 Gmail / Outlook / 企业邮箱这类 587 的配置一律连接失败——
        # 而设置页允许填任意端口，用户只会看到一句语焉不详的异常。
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as server:
                server.login(username, cfg["password"])
                server.sendmail(username, to_list, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=25) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(username, cfg["password"])
                server.sendmail(username, to_list, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, f"邮件发送异常：{e}"


def _enabled_channels(notify: dict) -> List[str]:
    channels = []
    if (notify.get("wxpusher") or {}).get("enabled"):
        channels.append("wxpusher")
    if (notify.get("smtp") or {}).get("enabled"):
        channels.append("smtp")
    return channels


def send_via_channels(title: str, content_md: str) -> List[dict]:
    """向所有启用渠道发送；返回每条渠道的结果（含未启用渠道的提示）。"""
    cfg = AutomationConfigManager().get_config()
    notify = cfg.get("notify") or {}
    results: List[dict] = []

    wx = notify.get("wxpusher") or {}
    if wx.get("enabled"):
        ok, detail = send_wxpusher(wx.get("app_token", ""), wx.get("uids", ""), title, content_md)
        results.append({"channel": "微信（WxPusher）", "ok": ok, "detail": detail})
        (logger.info if ok else logger.warning)(f"WxPusher 通知: ok={ok}, {detail}")

    smtp = notify.get("smtp") or {}
    if smtp.get("enabled"):
        ok, detail = send_email(smtp, title, content_md)
        results.append({"channel": "邮箱（SMTP）", "ok": ok, "detail": detail})
        (logger.info if ok else logger.warning)(f"SMTP 通知: ok={ok}, {detail}")

    if not results:
        results.append({"channel": "（无）", "ok": False, "detail": "尚未启用任何通知渠道"})
    return results


def send_summary(result: dict) -> List[dict]:
    """检查轮结束后的汇总通知。

    空轮静默：本轮一个新任务都没提交（submitted 为空，即 0 成功 0 失败，
    只有跳过）时不打扰用户，直接返回跳过标记。2026-10-01 用户投诉：连续收到
    「成功 0 / 失败 0（本轮没有需要新总结的视频）」的邮件，纯属噪音。
    注意：submitted 非空但全部 still_pending（超时未归）时仍要通知——
    那是异常状态，不是「没事发生」。
    """
    submitted = result.get("submitted") or []
    if not submitted:
        skipped = result.get("skipped") or []
        logger.info(f"空轮静默：本轮无新任务（跳过 {len(skipped)}），不发送汇总通知")
        return [{"channel": "（空轮静默）", "ok": True, "detail": f"本轮无新任务，跳过 {len(skipped)}，未打扰用户"}]
    ok = [s for s in submitted if s.get("status") == "SUCCESS"]
    failed = [s for s in submitted if s.get("status") == "FAILED"]
    pending = [s for s in submitted if s.get("status") not in ("SUCCESS", "FAILED")]
    skipped = result.get("skipped") or []

    lines = [f"【BiliNote 检查轮】{result.get('started_at', '')}"]
    line2 = f"新增 {len(submitted)}：成功 {len(ok)}，失败 {len(failed)}"
    if pending:
        line2 += f"，仍在处理 {len(pending)}"
    line2 += f"；跳过 {len(skipped)}"
    lines.append(line2)
    for s in ok:
        lines.append(f"✅ {s.get('title') or s.get('bvid')}")
    for s in failed:
        lines.append(f"❌ {s.get('title') or s.get('bvid')}：{str(s.get('message') or '')[:80]}")

    content = "\n".join(lines)
    title = f"BiliNote 检查轮：成功 {len(ok)} / 失败 {len(failed)}"
    return send_via_channels(title, content)


def send_test() -> List[dict]:
    """设置页「发送测试通知」：向启用渠道各发一条测试消息。"""
    return send_via_channels(
        "BiliNote 测试通知",
        "这是一条来自 BiliNote 的测试通知。\n看到它说明该渠道配置正确 ✅",
    )

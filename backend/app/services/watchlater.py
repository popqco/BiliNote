"""稍后再看（Watch Later）读取与列表链接识别。

自动化检查轮的唯一输入源；同时支撑「粘贴列表页链接批量导入」。
使用账号 Cookie（「设置 → 下载配置」里的 B 站 Cookie），SESSDATA 失效时
给出明确引导，而不是静默空列表。
"""
import re
from typing import List

import requests

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_WATCHLATER_RE = re.compile(r"bilibili\.com/list/watchlater", re.I)
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def is_watchlater_list_url(url: str) -> bool:
    """是否是「裸」的稍后再看列表页链接（不含具体 bvid）。

    带 bvid 的列表页链接（从列表页点进单个视频的地址）会被 normalize_video_url
    转成标准 /video/BVxxx，不在此列。
    """
    u = str(url or "")
    return bool(_WATCHLATER_RE.search(u)) and not _BV_RE.search(u)


def fetch_watchlater(ps: int = 20, max_items: int = 100) -> List[dict]:
    """拉取当前账号的「稍后再看」列表。

    返回 [{bvid,title,cover_url,duration,add_at,video_url}]；
    add_at 为加入时间的 unix 秒（用于「仅最近 N 天新增」模式）。
    Cookie 缺失/失效会抛出带操作指引的异常。
    """
    cookie = CookieConfigManager().get("bilibili") or ""
    if not cookie:
        raise RuntimeError("未配置 B 站 Cookie：请到「设置 → 下载配置」填写后重试")

    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/list/watchlater",
        "Cookie": cookie,
    }

    items: List[dict] = []
    page = 1
    while len(items) < max_items:
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/v2/history/toview/web",
                params={"ps": ps, "pn": page},
                headers=headers,
                timeout=12,
            )
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"请求稍后再看接口失败：{e}")

        code = data.get("code")
        if code != 0:
            if code == -101:
                raise RuntimeError("B 站 Cookie 未登录或已失效：请到「设置 → 下载配置」更新 SESSDATA")
            raise RuntimeError(f"稍后再看接口返回错误：code={code}, msg={data.get('message')}")

        d = data.get("data") or {}
        lst = d.get("list") or []
        if not lst:
            break
        for it in lst:
            bvid = it.get("bvid")
            if not bvid:
                continue
            items.append({
                "bvid": bvid,
                "title": it.get("title"),
                "cover_url": it.get("pic"),
                "duration": it.get("duration"),
                "add_at": it.get("add_at"),
                "video_url": f"https://www.bilibili.com/video/{bvid}",
            })
        total = int(d.get("count") or 0)
        if (total and len(items) >= total) or len(lst) < ps:
            break
        page += 1

    logger.info(f"稍后再看共取到 {len(items)} 个视频")
    return items[:max_items]

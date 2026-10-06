import re
from typing import Optional
import requests


def extract_video_id(url: str, platform: str) -> Optional[str]:
    """
    从视频链接中提取视频 ID

    :param url: 视频链接
    :param platform: 平台名（bilibili / youtube / douyin / xiaohongshu）
    :return: 提取到的视频 ID 或 None
    """
    if platform == "bilibili":
        # 如果是短链接，则解析真实链接
        if "b23.tv" in url:
            resolved_url = resolve_bilibili_short_url(url)
            if resolved_url:
                url = resolved_url

        # 匹配 BV号（如 BV1vc411b7Wa）
        match = re.search(r"BV([0-9A-Za-z]+)", url)
        return f"BV{match.group(1)}" if match else None

    elif platform == "youtube":
        # 匹配 v=xxxxx、youtu.be/xxxxx 或 shorts/xxxxx，ID 长度通常为 11
        match = re.search(r"(?:v=|youtu\.be/|shorts/)([0-9A-Za-z_-]{11})", url)
        return match.group(1) if match else None

    elif platform == "douyin":
        # 匹配 douyin.com/video/1234567890123456789
        match = re.search(r"/video/(\d+)", url)
        return match.group(1) if match else None

    elif platform == "xiaohongshu":
        # 小红书笔记 ID：/explore/<24hex> 或 /discovery/item/<24hex>；
        # xhslink.com 短链先 follow redirect（与 douyin 短链同理）
        candidate = url
        found = re.findall(
            r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
            url,
        )
        if found:
            candidate = found[0]
        if "xhslink.com" in candidate:
            try:
                candidate = requests.head(candidate, allow_redirects=True, timeout=15).url
            except Exception:
                return None
        match = re.search(r"(?:/explore/|/discovery/item/)([\da-fA-F]{24})", candidate)
        return match.group(1).lower() if match else None

    return None


# 多 P 视频下载器 id 带 _pN 后缀（yt-dlp 对分 P 视频报 BVxxx_p1）
_MULTI_P_SUFFIX = re.compile(r"_p\d+$", re.IGNORECASE)


def normalize_video_id(video_id: Optional[str], platform: str = "") -> Optional[str]:
    """把视频 id 归一化为裸 id（多 P 后缀 `_pN` 仅 bilibili 有，剥掉）。

    提交/自动化/状态文件全程用裸 bvid，唯独落库曾用下载器原始 id——
    自动化「已有成功笔记」按裸 bvid 查库永远查不到，多 P 视频每轮重跑
    （2026-10-06 潘通视频 15:47-16:53 连跑 6 次实锤）。
    youtube 的 11 位 id 理论上可能以 _p<数字> 结尾，故只在 bilibili 剥后缀。
    """
    if not video_id or platform != "bilibili":
        return video_id
    return _MULTI_P_SUFFIX.sub("", video_id)


def normalize_video_url(url: str) -> str:
    """
    将任意包含 BV 号的 B 站链接规范化为标准视频链接。

    支持稍后再看（/list/watchlater/?bvid=BV...）、收藏夹播放页（/list/mlXXX?bvid=BV...）、
    带追踪参数的分享链接等。保留分 P 参数，丢弃其余查询参数。

    b23.tv 短链与无 BV 号的链接原样返回（后者交由校验器拒绝）。
    """
    if "b23.tv" in url:
        return url

    match = re.search(r"BV([0-9A-Za-z]+)", url)
    if not match:
        return url

    normalized = f"https://www.bilibili.com/video/BV{match.group(1)}"
    p = extract_bilibili_p_number(url)
    if p:
        normalized += f"?p={p}"
    return normalized


def resolve_bilibili_short_url(short_url: str) -> Optional[str]:
    """
    解析哔哩哔哩短链接以获取真实视频链接

    :param short_url: Bilibili短链接（如"https://b23.tv/xxxxxx"）
    :return: 真实的视频链接或None
    """
    try:
        response = requests.head(short_url, allow_redirects=True)
        return response.url
    except requests.RequestException as e:
        print(f"Error resolving short URL: {e}")
        return None


def extract_bilibili_p_number(url: str) -> Optional[int]:
    """
    从 B 站分 P 视频 URL 中提取 p 参数（分 P 序号）。

    支持格式：
      - https://www.bilibili.com/video/BVxxx/?p=36
      - https://www.bilibili.com/video/BVxxx?p=5
      - https://b23.tv/xxxxx?p=10
      - https://www.bilibili.com/video/BVxxx/pN (尾缀形式)

    :param url: B 站视频链接
    :return: 分 P 序号（从 1 开始），非分 P 视频返回 None
    """
    if "b23.tv" in url:
        url = resolve_bilibili_short_url(url) or url

    # 匹配 ?p=NNN 或 &p=NNN
    match = re.search(r'[?&]p=(\d+)', url)
    if match:
        p = int(match.group(1))
        if p >= 1:
            return p

    # 匹配 /pN 尾缀形式（较少见）
    match = re.search(r'/p(\d+)(?:/?$|\?|&)', url)
    if match:
        p_val = int(match.group(1))
        if p_val >= 1:
            return p_val

    return None

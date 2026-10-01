import re


def prepend_source_link(markdown: str | None, source_url: str) -> str | None:
    """
    在笔记开头添加来源链接；若首个非空行已包含来源链接，则更新该行并避免重复。
    """
    if markdown is None:
        return None

    source = (source_url or "").strip()
    if not source:
        return markdown

    header = f"> 来源链接：{source}"
    lines = markdown.splitlines()
    first_non_empty_idx = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_non_empty_idx = idx
            break

    if first_non_empty_idx is not None:
        first_line = lines[first_non_empty_idx].strip()
        if first_line.startswith("> 来源链接：") or first_line.startswith("来源链接："):
            lines[first_non_empty_idx] = header
            return "\n".join(lines)

    if markdown.strip():
        return f"{header}\n\n{markdown}"
    return header


def replace_content_markers(markdown: str, video_id: str, platform: str = 'bilibili') -> str:
    """
    替换 *Content-04:16*、Content-04:16 或 Content-[04:16] 为超链接，跳转到对应平台视频的时间位置

    video_id 可能带分 P 后缀（`BV1xx_p3`），bilibili 需要把 p 转成查询参数；
    时间跳转参数在已有 `?` 时用 `&t=`、否则用 `?t=`，否则链接会被拼成 `.../BV1xx&t=120`
    这种打不开的地址。
    """
    # 匹配三种形式：*Content-04:16*、Content-04:16、Content-[04:16]
    pattern = r"(?:\*?)Content-(?:\[(\d{2}):(\d{2})\]|(\d{2}):(\d{2}))"

    # 不再在闭包里给 video_id 赋值：那会把外层的 video_id 变成 replacer 的局部变量，
    # 读它就抛 UnboundLocalError，整个 re.sub 中断 → 每条笔记都静默丢掉全部时间跳转
    # （调用处 except 只打一行 warning）。预处理放在闭包外面，闭包只读。
    safe_video_id = (video_id or "").strip()

    def replacer(match):
        mm = match.group(1) or match.group(3)
        ss = match.group(2) or match.group(4)
        total_seconds = int(mm) * 60 + int(ss)
        if not safe_video_id:
            return f"({mm}:{ss})"

        if platform == 'bilibili':
            vid = safe_video_id.replace("_p", "?p=")
            sep = "&" if "?" in vid else "?"
            url = f"https://www.bilibili.com/video/{vid}{sep}t={total_seconds}"
        elif platform == 'youtube':
            url = f"https://www.youtube.com/watch?v={safe_video_id}&t={total_seconds}s"
        elif platform == 'douyin':
            url = f"https://www.douyin.com/video/{safe_video_id}"
            return f"[原片 @ {mm}:{ss}]({url})"
        else:
            return f"({mm}:{ss})"

        return f"[原片 @ {mm}:{ss}]({url})"

    return re.sub(pattern, replacer, markdown)


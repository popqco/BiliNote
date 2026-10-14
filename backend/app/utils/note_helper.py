import re


def normalize_math_delimiters(markdown: str | None) -> str | None:
    """
    把 AI 写出的各类 LaTeX 定界符统一成 remark-math 唯一能识别的两种：
    行内 `\\(...\\)` → `$...$`，块级 `\\[...\\]` → `$$...$$`。

    背景：prompt 之前只写「以 LaTeX 呈现」，模型习惯性输出 \\(...\\) / \\[...\\]；
    而前端 remark-math@5 只认 `$` / `$$`，其它写法会被 rehype-katex 直接跳过，
    以整段原文显示在笔记里（2026-10-01 用户实拍：LTspice 笔记公式全是原文）。

    只做定界符替换，不碰公式本体：
    - 先处理块级 `\\[ ... \\]`（多行，DOTALL），再处理行内 `\\( ... \\)`；
    - 已经是 `$` / `$$` 的不动；
    - markdown 为 None 时原样返回（与本文件其它函数约定一致）。
    """
    if markdown is None:
        return None
    if not markdown:
        return markdown

    # 块级：\[ ... \] → $$...$$。用非贪婪匹配，跨行（DOTALL）。
    normalized = re.sub(
        r"\\\[(.+?)\\\]",
        lambda m: f"$${m.group(1)}$$",
        markdown,
        flags=re.DOTALL,
    )
    # 行内：\( ... \) → $...$。同行内单行公式，不跨行，避免吞掉后面的正文。
    normalized = re.sub(
        r"\\\((.+?)\\\)",
        lambda m: f"${m.group(1)}$",
        normalized,
    )
    return normalized


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


def cleanup_marker_residue(markdown: str) -> str:
    """清理截图/原片标记替换后残留的孤立星号。

    模型有时把标记写成斜体包裹形态（`*Content-[02:10]*`），替换只消费标记
    本体，留下包在外面的 `*`——用户看到的就是"标题后多个星号"、"段落之间
    一行孤零零的 *"、"图片后挂个 *"（2026-10-05 用户截图实锤）。三条规则都很保守：
    1. 整行只有 1-2 个星号加空白 → 整行删除（合法 markdown 不存在这种行）；
    2. 含视频跳转链接的行尾跟 1-2 个星号 → 摘掉星号（闭合被标记打断的斜体）；
    3. 图片链接行尾跟 1-2 个星号 → 摘掉星号但保留图片（截图必须留在正文里）。
    """
    if not markdown:
        return markdown
    # 整行孤星
    cleaned = re.sub(r"(?m)^[ \t]*\*{1,2}[ \t]*\r?\n", "", markdown)
    # 行尾孤星（仅限含跳转链接的行，避免误伤正常斜体收尾）。
    # 2026-10-14 补 YouTube：时间链接是 `?v=…&t=0s`，旧正则只认 `?t=`/`/video/`，
    # YouTube 笔记里标题行尾的残留星号一直没被摘掉（实拍：`## 1. xxx [原片 @ 00:00](…&t=0s)*`）。
    cleaned = re.sub(
        r"(?m)^([^\n]*\[(?:原片|▶)?[^\]]*\]\([^\)]*(?:[?&]t=|/video/)[^\)]*\))\s*\*{1,2}[ \t]*$",
        r"\1",
        cleaned,
    )
    # 图片被星号包住的形态先解包（也是标记替换的残留之一：`*![..](..)*`），
    # 解完剩下的行尾孤星才能无歧义摘掉
    cleaned = re.sub(r"\*{1,2}(!\[[^\]]*\]\([^\)]*\))\*{1,2}", r"\1", cleaned)
    # 图片行尾孤星（图片本体保留，只摘残留星号）。
    # 2026-10-15：旧规则要求整行以图片开头，列表项「- 文本![](img)*」形态整行不匹配，
    # 江油笔记里成对标记（`*Screenshot-A* *Screenshot-B*`）替换后残留的星号全部漏网。
    # 放宽为「图片后紧跟 1-2 个星号到行尾」（上面的解包已保证图片不会被斜体包裹）。
    cleaned = re.sub(
        r"(!\[[^\]]*\]\([^\)]*\))[ \t]*\*{1,2}[ \t]*$",
        r"\1",
        cleaned,
        flags=re.MULTILINE,
    )
    # 两图之间的孤星：模型把相邻两个截图标记都写成 `*Screenshot-..*` 时，
    # 替换标记本体后两张图之间会留一个 `*`，在渲染层开启一段跨行的斜体
    # （2026-10-15 江油笔记实拍 10 处）。只在星号两侧都是图片时摘除，
    # 不碰 `**加粗**` 等正常用法。
    cleaned = re.sub(
        r"(!\[[^\]]*\]\([^\)]*\))[ \t]*\*{1,2}[ \t]*(?=!\[)",
        r"\1 ",
        cleaned,
    )
    return cleaned


# ---------------- 目录 / AI 总结「后整理」（2026-10-14） ----------------
# 背景：分批喂图时每个批次都会按格式要求产出自己的「## 目录」与「## AI 总结」
# （且带着「本段/本批」口吻），55 批经多层合并后这些段落会成堆残留——用户看到
# 的 AI 总结是十几段「本段指出…」的拼贴。prompt 层面的去重指令模型经常不遵守，
# 唯一可靠的收敛点是合并完成后做确定性整理：摘掉旧的，重建目录、重写一份总结。

_SECTION_TITLE_NOISE = re.compile(r"[\s:：*]+")


def _norm_section_title(title: str) -> str:
    return _SECTION_TITLE_NOISE.sub("", title or "").lower()


def strip_global_sections(markdown: str | None, names=("目录", "AI 总结")) -> tuple[str, str]:
    """移除「## 目录」「## AI 总结」段落，返回 (剩余正文, 被移除的总结正文)。

    标题按「去空白/标点后前缀匹配」识别（`## AI总结`、`## AI 总结：` 都能认），
    段落内容直到下一个同级或更高级标题为止。被移除的总结正文拼起来返回，
    供调用方在重写总结失败时兜底。
    """
    if not markdown:
        return markdown or "", ""
    targets = [_norm_section_title(n) for n in names]
    summary_key = _norm_section_title("AI 总结")
    lines = markdown.split("\n")
    out: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[i])
        hit = None
        if m:
            norm = _norm_section_title(m.group(2))
            hit = next((t for t in targets if norm.startswith(t)), None)
        if not hit:
            out.append(lines[i])
            i += 1
            continue
        level = len(m.group(1))
        i += 1
        buf: list[str] = []
        while i < len(lines):
            m2 = re.match(r"^(#{1,6})\s+", lines[i])
            if m2 and len(m2.group(1)) <= level:
                break
            buf.append(lines[i])
            i += 1
        if hit == summary_key:
            text = "\n".join(buf).strip()
            if text:
                removed.append(text)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return body, "\n\n".join(removed)


_H2_LINE = re.compile(r"^##\s+(.*?)\s*$", re.M)
_CONTENT_MARKER = re.compile(
    r"\*?Content-(?:\[(\d{1,3}:\d{2}(?::\d{2})?)\]|(\d{1,3}:\d{2}(?::\d{2})?))\*?"
)
_ORIGIN_LINK = re.compile(r"\[原片 @ (\d{1,3}:\d{2}(?::\d{2})?)\]\(([^)]+)\)")


def build_toc_block(body: str, link_enabled: bool = True) -> str | None:
    """从正文的二级标题重建「## 目录」段落（确定性，不调模型）。

    条目 = 标题（保留模型写的编号）+ 时间入口：
    - 标题里已有 `[原片 @ mm:ss](url)`（存量笔记）→ 原样带上；
    - 只有 `*Content-[mm:ss]` 标记时：link_enabled 则保留标记（后续
      replace_content_markers 会换成跳转链接），否则写成纯文本（mm:ss）。
    有效标题少于 2 个时返回 None（不生成空目录）。
    """
    entries: list[str] = []
    for raw_title in _H2_LINE.findall(body or ""):
        title = raw_title
        time_label: str | None = None
        url: str | None = None
        m = _ORIGIN_LINK.search(title)
        if m:
            time_label, url = m.group(1), m.group(2)
            title = title[: m.start()] + title[m.end():]
        else:
            m = _CONTENT_MARKER.search(title)
            if m:
                time_label = m.group(1) or m.group(2)
                title = title[: m.start()] + title[m.end():]
        title = title.strip().strip("*").strip()
        if not title:
            continue
        if url:
            entries.append(f"- {title} [原片 @ {time_label}]({url})")
        elif time_label and link_enabled:
            entries.append(f"- {title} *Content-[{time_label}]")
        elif time_label:
            entries.append(f"- {title}（{time_label}）")
        else:
            entries.append(f"- {title}")
    if len(entries) < 2:
        return None
    return "## 目录\n\n" + "\n".join(entries)


def replace_content_markers(markdown: str, video_id: str, platform: str = 'bilibili') -> str:
    """
    替换 *Content-04:16*、Content-04:16 或 Content-[04:16] 为超链接，跳转到对应平台视频的时间位置

    video_id 可能带分 P 后缀（`BV1xx_p3`），bilibili 需要把 p 转成查询参数；
    时间跳转参数在已有 `?` 时用 `&t=`、否则用 `?t=`，否则链接会被拼成 `.../BV1xx&t=120`
    这种打不开的地址。

    时间写法两种都要认：`MM:SS`（分钟可以超过 59，3 小时的视频模型常写
    `Content-[101:52]`）和 `H:MM:SS`。只认两位分钟会漏掉大半标记——
    实测一条 2h53m 的笔记 46 个标记里 15 个是 `101:52` 这种三位的。
    """
    pattern = (
        r"(?:\*?)Content-(?:"
        r"\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]"
        r"|(\d{1,3}):(\d{2})(?::(\d{2}))?"
        r")"
    )

    # 不再在闭包里给 video_id 赋值：那会把外层的 video_id 变成 replacer 的局部变量，
    # 读它就抛 UnboundLocalError，整个 re.sub 中断 → 每条笔记都静默丢掉全部时间跳转
    # （调用处 except 只打一行 warning）。预处理放在闭包外面，闭包只读。
    safe_video_id = (video_id or "").strip()

    def replacer(match):
        mm, ss, hh = match.group(1), match.group(2), match.group(3)
        if mm is None:                      # 无方括号的那一支
            mm, ss, hh = match.group(4), match.group(5), match.group(6)
        if hh is not None:
            total_seconds = int(mm) * 3600 + int(ss) * 60 + int(hh)
            label = f"{mm}:{ss}:{hh}"
        else:
            total_seconds = int(mm) * 60 + int(ss)
            label = f"{mm}:{ss}"
        if not safe_video_id:
            return f"({label})"

        if platform == 'bilibili':
            vid = safe_video_id.replace("_p", "?p=")
            sep = "&" if "?" in vid else "?"
            url = f"https://www.bilibili.com/video/{vid}{sep}t={total_seconds}"
        elif platform == 'youtube':
            url = f"https://www.youtube.com/watch?v={safe_video_id}&t={total_seconds}s"
        elif platform == 'douyin':
            url = f"https://www.douyin.com/video/{safe_video_id}"
            return f"[原片 @ {label}]({url})"
        else:
            return f"({label})"

        return f"[原片 @ {label}]({url})"

    return re.sub(pattern, replacer, markdown)


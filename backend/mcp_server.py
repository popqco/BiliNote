"""BiliNote MCP Server —— 把 BiliNote 的笔记库与功能暴露给外部 AI harness（Codex/ZCode 等）。

设计定位：harness 优先。本 server 只做"数据与操作通道"，检索走纯向量召回的
/api/chat/search（本地嵌入、零在线 API），原始片段交给 harness 自己的强模型推理；
BiliNote 内置的在线 API 问答（ask_notes）仅作兜底，不作首选。

运行方式（沿用 automation_cli.py 先例：共享 venv 直接跑脚本，无需打包）：
    <venv>/Scripts/python.exe backend/mcp_server.py

环境变量：
    BILINOTE_BASE_URL  后端地址，默认 http://127.0.0.1:8483（本机回环免配对鉴权）。
                       远程 Worker 场景填 http://<tailscale-ip>:8483 并配 BILINOTE_TOKEN。
    BILINOTE_TOKEN     配对 token（可选）：非回环访问时自动转为 X-Pairing-Token 头。

接入示例：
    ZCode  MCP 配置: command=<venv python>, args=[<repo>/backend/mcp_server.py]
    Codex  ~/.codex/config.toml:
        [mcp_servers.bilinote]
        command = "C:\\...\\python.exe"
        args = ["C:\\...\\backend\\mcp_server.py"]
"""

import json
import os
import re
import sys
from datetime import datetime
from typing import Literal, Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("BILINOTE_BASE_URL", "http://127.0.0.1:8483").rstrip("/")
TOKEN = os.environ.get("BILINOTE_TOKEN", "").strip()

# 下载/生成类请求服务端可能排队较久，问答链路（ask）前端超时是 120s，
# 这里统一放宽到 180s，connect 单独收紧便于快速报"后端没开"。
_TIMEOUT = httpx.Timeout(180.0, connect=5.0)

mcp = FastMCP(
    "bilinote",
    instructions=(
        "BiliNote 视频笔记库（B站/YouTube/公众号等视频的 AI 笔记）。"
        "回答笔记相关问题时：优先 search_notes 检索原文；命中即可回答，不要为了确认再 list_notes。"
        "需要更多上下文时，先用更具体的关键词再 search 一次；仍不够再用 "
        "get_note(section=\"小节标题关键词\") 精确取节。"
        "get_note 默认 qa 模式（省略截图与 AI 总结），整篇阅读或导出时用 format=\"full\"；"
        "长笔记先 note_outline。ask_notes 是软件内置在线问答，仅在用户明确要求时作兜底。"
    ),
)


class BiliNoteError(Exception):
    """带业务语义的错误：msg 面向 harness 可读。"""


def _is_loopback_url(url: str) -> bool:
    """url 是否指向本机回环（127.0.0.1 / localhost / ::1）。"""
    import urllib.parse

    try:
        host = urllib.parse.urlparse(str(url or "")).hostname or ""
    except Exception:
        return False
    return host.lower() in ("127.0.0.1", "localhost", "::1")


def _client() -> httpx.Client:
    headers = {}
    if TOKEN:
        headers["X-Pairing-Token"] = TOKEN
    kwargs = {}
    if _is_loopback_url(BASE_URL):
        # 回环地址永远直连：httpx 的 trust_env 在 Windows 上会读系统注册表代理
        # 且不尊重 ProxyOverride 例外列表——Clash 等系统代理开着时，
        # http://127.0.0.1:8483 会被塞给代理层（7892）拿回 502 空响应
        # （2026-10-16 实拍：MCP list_notes 全挂在 502；与后端 a6b2523
        # 「回环不走代理」同一根因）。trust_env=False 关掉 env+注册表两路代理。
        kwargs["trust_env"] = False
    return httpx.Client(base_url=BASE_URL, timeout=_TIMEOUT, headers=headers, **kwargs)


def _call(method: str, path: str, payload: Optional[dict] = None) -> dict:
    """调后端 REST API，返回完整信封 {code, msg, data}。

    网络层失败转成带指引的人话错误——后端未启动是最常见故障
    （BiliNoteBackend 随桌面端退出，桌面端没开时本 server 依然能被连上）。
    """
    try:
        with _client() as c:
            resp = c.request(method, path, json=payload)
    except httpx.ConnectError:
        raise BiliNoteError(
            f"无法连接 BiliNote 后端（{BASE_URL}）。请先启动 BiliNote 桌面端；"
            "若后端端口被改过，请在 MCP 配置中设置 BILINOTE_BASE_URL。"
        )
    except httpx.HTTPError as e:
        raise BiliNoteError(f"请求 BiliNote 后端失败：{type(e).__name__}: {e}")

    if resp.status_code == 404:
        raise BiliNoteError(
            f"接口不存在（404）：{path}。部署的 BiliNoteBackend 可能是旧版本，请升级后端。"
        )
    if resp.status_code == 401:
        raise BiliNoteError("未配对（401）：远程访问需要正确的 BILINOTE_TOKEN。")
    if resp.status_code == 403:
        raise BiliNoteError(
            "被拒绝（403）：Worker 关闭了远端配置开关，该操作只允许在 Worker 本机（回环）执行。"
        )

    ctype = resp.headers.get("content-type", "")
    if "application/json" not in ctype:
        resp.raise_for_status()
        return {"code": 0, "msg": "success", "data": resp.content}

    body = resp.json()
    # 信封约定：code==0 成功；code!=0 失败（HTTP 状态多为 200）。
    if body.get("code") not in (0, None):
        err = BiliNoteError(str(body.get("msg") or f"code={body.get('code')}"))
        err.code = body.get("code")
        err.data = body.get("data")
        raise err
    return body


def _data(method: str, path: str, payload: Optional[dict] = None):
    body = _call(method, path, payload)
    return body.get("data")


def _note_markdown(result: dict) -> str:
    """笔记 result.markdown 兼容 str 与多版本 list 两种形态，取最新版。"""
    md = result.get("markdown", "")
    if isinstance(md, list):
        md = (md[-1] or {}).get("content", "") if md else ""
    return md or ""


def _fmt_duration(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except Exception:
        return "-"
    if not seconds:
        return "-"
    m, s = divmod(seconds, 60)
    return f"{m}分{s}秒" if m else f"{s}秒"


def _fmt_ts(seconds) -> str:
    try:
        seconds = int(float(seconds or 0))
    except Exception:
        return "-"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------- 输出整形

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_AI_SUMMARY_RE = re.compile(r"(?im)^##\s+AI\s*总结\s*$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_DEBUG = os.environ.get("BILINOTE_MCP_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _ret(tool: str, text: str) -> str:
    """统一返回；BILINOTE_MCP_DEBUG=1 时把结果字符数打到 stderr（不污染 stdio 协议）。"""
    if _DEBUG:
        print(f"[bilinote-mcp] {tool}: {len(text)} chars", file=sys.stderr, flush=True)
    return text


def _strip_images(md: str) -> str:
    """去掉 markdown 图片链接；问答时截图链接对模型是噪音，UI 仍保留原文。"""
    cleaned = _IMG_RE.sub("", md)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _strip_ai_summary(md: str) -> str:
    """去掉结尾的 AI 总结小节；它是正文的二次压缩，问答时冗余。"""
    return _AI_SUMMARY_RE.split(md, maxsplit=1)[0].rstrip()


def _split_sections(md: str) -> list[tuple[str, str]]:
    """按二级标题切分，返回 [(标题, 含标题的整节文本)]。"""
    sections: list[tuple[str, str]] = []
    heading: Optional[str] = None
    buf: list[str] = []
    for line in md.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            if heading is not None:
                sections.append((heading, "\n".join(buf).strip()))
            heading = m.group(1).strip()
            buf = [line]
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buf).strip()))
    return sections


def _normalize_match(text: str) -> str:
    """标题匹配用：忽略空白、标点、引号和 markdown 装饰符。"""
    text = (text or "").lower()
    return re.sub(
        r"[\s*_`~\"'“”‘’：:，,。.、；;！!？?（）()\[\]【】<>《》\-—–/\\]+", "", text
    )


def _is_synthetic_section(title: str) -> bool:
    n = _normalize_match(title)
    return any(k in n for k in ("ai总结", "目录", "tableofcontents", "toc"))


def _find_section(
    md: str, query: str
) -> tuple[Optional[tuple[str, str]], list[tuple[str, str]], int]:
    """返回 (命中的小节, 全部小节, 命中数)；未命中时第一项为 None。"""
    sections = _split_sections(md)
    q = _normalize_match(query)
    if not q:
        return None, sections, 0
    exact = [s for s in sections if _normalize_match(s[0]) == q]
    if exact:
        return exact[0], sections, len(exact)
    partial = [s for s in sections if q in _normalize_match(s[0])]
    if partial:
        return partial[0], sections, len(partial)
    return None, sections, 0


def _section_titles(sections: list[tuple[str, str]], limit: int = 20) -> str:
    lines = [f"- {title}" for title, _ in sections[:limit]]
    if len(sections) > limit:
        lines.append(f"- …其余 {len(sections) - limit} 节")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int, hint: str = "") -> str:
    if not max_chars or len(text) <= max_chars:
        return text
    original_len = len(text)
    cut = text[: max(1, max_chars)]
    nl = cut.rfind("\n")
    if nl > max_chars * 0.6:
        cut = cut[:nl]
    marker = f"\n\n…（已截断，原长 {original_len} 字符"
    if hint:
        marker += f"；{hint}"
    marker += "）"
    return cut.rstrip() + marker


def _fmt_datetime(value) -> str:
    """把 Unix 秒/毫秒时间戳转成本地可读时间；非法值原样返回。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return str(value or "-")
    if ts > 1e12:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(value or "-")


def _status_text(task_id: str, data: dict) -> str:
    return (
        f"任务 {task_id} 尚未完成：status={data.get('status', 'UNKNOWN')}，"
        f"message={data.get('message') or '-'}。"
        "进行中可稍后重试本工具轮询；FAILED 可用 retry_task 重试。"
    )


def _render_note(
    data: dict,
    task_id: str,
    *,
    section: Optional[str] = None,
    fmt: str = "qa",
    include_screenshots: bool = False,
    include_ai_summary: bool = False,
    max_chars: Optional[int] = None,
) -> str:
    result = data.get("result", {}) or {}
    meta = result.get("audio_meta", {}) or {}
    raw = meta.get("raw_info", {}) or {}
    md = _note_markdown(result)
    if not md:
        return f"任务 {task_id} 状态为 SUCCESS 但笔记内容为空。"

    header = (
        f"# {meta.get('title') or '(无标题)'}\n\n"
        f"- 平台：{meta.get('platform', '-')} | 时长：{_fmt_duration(meta.get('duration'))}\n"
        f"- 作者：{raw.get('uploader', '-')} | 链接：{raw.get('webpage_url') or '-'}\n"
        f"- task_id：`{task_id}`\n\n---\n\n"
    )

    body = md
    notes: list[str] = []
    section_title = ""
    if section:
        found, sections, matches = _find_section(body, section)
        if not found:
            return header + "未找到匹配小节。可用小节：\n" + _section_titles(sections)
        section_title = found[0]
        body = found[1]
        notes.append(f"仅显示小节：{section_title}；需要全文用 format=\"full\"。")
        if matches > 1:
            notes.append(
                f"匹配到 {matches} 节，已返回第一节；可用更具体的 section 精确取节。"
            )

    is_ai_section = bool(section_title) and "ai总结" in _normalize_match(section_title)
    had_images = bool(_IMG_RE.search(body))
    had_ai_summary = bool(_AI_SUMMARY_RE.search(body)) and not is_ai_section
    if fmt == "qa":
        if not include_ai_summary and not is_ai_section:
            body = _strip_ai_summary(body)
        if not include_screenshots:
            body = _strip_images(body)
        stripped = []
        if had_images and not include_screenshots:
            stripped.append("截图")
        if had_ai_summary and not include_ai_summary:
            stripped.append("AI 总结")
        if stripped:
            notes.append(
                f"qa 模式：已省略{'、'.join(stripped)}；需要全文用 format=\"full\"。"
            )

    prefix = "".join(f"> {n}\n>\n" for n in notes)
    content = header + prefix + body
    return _truncate(
        content,
        max_chars or 0,
        hint='需要指定小节用 section="标题关键词"',
    )


def _render_outline(data: dict, task_id: str) -> str:
    result = data.get("result", {}) or {}
    meta = result.get("audio_meta", {}) or {}
    md = _note_markdown(result)
    if not md:
        return f"任务 {task_id} 状态为 SUCCESS 但笔记内容为空。"

    sections = _split_sections(md)
    visible = [
        (title, body)
        for title, body in sections
        if _normalize_match(title) not in ("目录", "tableofcontents", "toc")
    ]
    lines = [
        f"# {meta.get('title') or '(无标题)'}",
        (
            f"- 平台：{meta.get('platform', '-')} | 时长：{_fmt_duration(meta.get('duration'))}"
            f" | task_id：`{task_id}`"
        ),
        f"- 正文长度：{len(md)} 字符 | 小节数：{len(visible)}",
        "",
        "小节：",
    ]
    for i, (title, body) in enumerate(visible, 1):
        ts = ""
        m = re.search(r"[（(](\d{1,2}:\d{2})[）)]", title)
        if m:
            ts = f"[{m.group(1)}] "
        clean = title.rstrip("* ").strip()
        tag = "[合成] " if "ai总结" in _normalize_match(title) else ""
        content_only = body.split("\n", 1)[1] if "\n" in body else ""
        lines.append(f"{i}. {ts}{tag}{clean} — {len(content_only)} 字符")
    lines.append("")
    lines.append(
        "提示：用 get_note(task_id, section=\"小节标题关键词\") 取指定小节；"
        "需要全文用 format=\"full\"。"
    )
    return "\n".join(lines)


def _search_keep(
    r: dict, source: str, include_synthetic: bool, section: Optional[str]
) -> bool:
    st = (r.get("source_type") or "").lower()
    if source == "note" and st != "markdown":
        return False
    if source == "transcript" and st != "transcript":
        return False
    title = r.get("section_title") or ""
    if not include_synthetic and _is_synthetic_section(title):
        return False
    if section and _normalize_match(section) not in _normalize_match(title):
        return False
    return True


def _search_empty_message(query: str, source: str, top_k: int) -> str:
    if source == "note":
        extra = '已只查笔记正文并过滤 "AI 总结"/"目录"；可试 source="all" 或 include_synthetic=true。'
    elif source == "transcript":
        extra = '已只查转写原片；笔记正文可改回 source="note"。'
    else:
        extra = "可换更具体的关键词，或调大 top_k（≤30）。"
    return (
        f"「{query}」没有命中（source={source}，top_k={top_k}）。{extra}"
        "若笔记未建索引，用 index_status 查看、reindex_notes 补建。"
    )


def _detect_platform(url: str) -> str:
    u = url.lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "douyin.com" in u:
        return "douyin"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "xiaohongshu"
    raise BiliNoteError(
        "无法从链接识别平台（支持 bilibili / youtube / douyin / xiaohongshu），"
        "请用 platform 参数显式指定。"
    )


def _extract_models(data) -> list:
    """兼容模型接口的两种返回形态：model_enable 是裸 list，
    model_list/{pid} 是 {"models": [...]} 字典（历史行为）。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("models", "data", "items"):
            v = data.get(key)
            if isinstance(v, list) and v:
                return v
    return []


def _default_provider_model() -> tuple[str, str]:
    """自动挑一个可用 provider + model（第一个 enabled 的供应商的第一个启用模型）。"""
    providers = _data("GET", "/api/get_all_providers") or []
    if not providers:
        raise BiliNoteError(
            "BiliNote 尚未配置任何 LLM 供应商：请在 BiliNote 设置页添加后重试。"
        )
    enabled = [p for p in providers if p.get("enabled")]
    provider = (enabled or providers)[0]

    models: list = []
    for path in (f"/api/model_enable/{provider['id']}", f"/api/model_list/{provider['id']}"):
        try:
            models = _extract_models(_data("GET", path))
        except BiliNoteError:
            models = []
        if models:
            break
    if not models:
        raise BiliNoteError(
            f"供应商「{provider.get('name')}」下没有可用模型：请在 BiliNote 设置页添加模型。"
        )
    first = models[0]
    model_name = first.get("model_name") if isinstance(first, dict) else str(first)
    return str(provider["id"]), model_name


def _resolve_provider_model(provider_id: Optional[str], model_name: Optional[str]):
    if provider_id and model_name:
        return provider_id, model_name
    dp, dm = _default_provider_model()
    return provider_id or dp, model_name or dm


def _recent_tasks(limit: int = 300) -> list[dict]:
    data = _data("GET", f"/api/tasks/recent?limit={max(1, min(limit, 300))}") or {}
    return data.get("tasks", []) or []


def _get_note_or_raise(task_id: str) -> dict:
    """取 task_status 信封 data；FAILED 但留有旧结果时把结果一并带回。"""
    try:
        return _data("GET", f"/api/task_status/{task_id}") or {}
    except BiliNoteError as e:
        old = getattr(e, "data", None) or {}
        if old.get("result"):
            return {"status": "FAILED", "result": old["result"], "message": str(e)}
        raise


# ---------------------------------------------------------------- 工具定义


@mcp.tool(structured_output=False)
def list_notes(limit: int = 10, status: str = "all", compact: bool = True) -> str:
    """浏览/发现笔记（按最近更新排序）。已知 task_id 时不要调用本工具。

    Args:
        limit: 最多返回条数（1-300，默认 10）。
        status: 过滤状态：all / SUCCESS / FAILED 或进行中状态（默认 all）。
        compact: 默认 True，省略视频链接，只保留标题/平台/状态/时间/task_id。

    Returns:
        每行一条：标题、平台、状态、更新时间、task_id。
    """
    tasks = _recent_tasks(limit=300)
    if status and status != "all":
        tasks = [t for t in tasks if t.get("status") == status]
    tasks = tasks[: max(1, min(limit, 300))]
    if not tasks:
        return _ret("list_notes", "笔记库为空（或没有匹配状态的笔记）。")

    lines = []
    for t in tasks:
        title = t.get("title") or "(无标题)"
        line = (
            f"- {title} | {t.get('platform', '-')} | {t.get('status')} "
            f"| {_fmt_datetime(t.get('updated_at'))} | task_id=`{t.get('task_id')}`"
        )
        if not compact:
            line += f" | {t.get('video_url', '')}"
        lines.append(line)
    header = f"最近 {len(tasks)} 条笔记（按更新排序）："
    if any(t.get("status") == "FAILED" for t in tasks):
        header += "（含失败任务，可用 retry_task 重试）"
    return _ret("list_notes", header + "\n" + "\n".join(lines))


@mcp.tool(structured_output=False)
def get_note(
    task_id: str,
    section: Optional[str] = None,
    format: Literal["qa", "full"] = "qa",
    include_screenshots: bool = False,
    include_ai_summary: bool = False,
    max_chars: Optional[int] = None,
) -> str:
    """读取笔记正文；默认 qa 模式，只给模型真正需要的正文。

    Args:
        task_id: 笔记任务 ID（list_notes / search_notes 返回的 task_id）。
        section: 只取标题匹配的小节；支持关键词或时间戳（如 "对策"、"02:46"）。
        format: "qa"（默认）省略截图链接与 AI 总结；"full" 返回完整原文。
        include_screenshots: qa 模式下也保留截图链接（默认 False）。
        include_ai_summary: qa 模式下也保留 AI 总结（默认 False）。
        max_chars: 截断到约这么多字符（按行边界），默认不截断。

    Returns:
        SUCCESS：元数据 + 笔记正文（或指定小节）。
        未完成/失败：当前状态、进度消息（可据此轮询或 retry_task）。
    """
    data = _get_note_or_raise(task_id)
    if data.get("status", "UNKNOWN") != "SUCCESS":
        return _ret("get_note", _status_text(task_id, data))
    text = _render_note(
        data,
        task_id,
        section=section,
        fmt=format,
        include_screenshots=include_screenshots,
        include_ai_summary=include_ai_summary,
        max_chars=max_chars,
    )
    return _ret("get_note", text)


@mcp.tool(structured_output=False)
def note_outline(task_id: str) -> str:
    """返回笔记目录（小节标题 + 时间戳 + 字符数），长笔记先看目录再取节。

    Args:
        task_id: 笔记任务 ID。

    Returns:
        小节列表；配合 get_note(task_id, section="...") 精确读取。
    """
    data = _get_note_or_raise(task_id)
    if data.get("status", "UNKNOWN") != "SUCCESS":
        return _ret("note_outline", _status_text(task_id, data))
    return _ret("note_outline", _render_outline(data, task_id))


@mcp.tool(structured_output=False)
def get_transcript(
    task_id: str,
    start_sec: float = 0,
    end_sec: Optional[float] = None,
    max_chars: Optional[int] = None,
) -> str:
    """读取一篇笔记对应的视频转写文本（按时间排序的分段原话）。

    Args:
        task_id: 笔记任务 ID。
        start_sec: 起始秒（默认 0）。
        end_sec: 结束秒（默认到结尾）。
        max_chars: 截断到约这么多字符（默认不截断；优先用时间窗收窄）。

    Returns:
        `[mm:ss] 原话` 逐行文本。适合核对视频里的原始表述、引用原话。
    """
    data = _get_note_or_raise(task_id)
    result = data.get("result", {}) or {}
    transcript = result.get("transcript", {}) or {}
    segments = transcript.get("segments", []) or []
    if not segments:
        return _ret("get_transcript", f"任务 {task_id} 没有可用的转写内容。")

    out = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        if start < float(start_sec):
            continue
        if end_sec is not None and start > float(end_sec):
            continue
        out.append(f"[{_fmt_ts(start)}] {seg.get('text', '')}")
    if not out:
        return _ret(
            "get_transcript", f"时间窗 [{start_sec}, {end_sec}] 内没有转写片段。"
        )
    total = len(out)
    if total > 500:
        out = out[:500] + [f"…（超出 500 行已截断，共 {total} 行，可缩小时间窗）"]
    lang = transcript.get("language") or ""
    header = f"转写（{lang or '语言未知'}，{total} 段）：\n"
    text = header + "\n".join(out)
    return _ret(
        "get_transcript",
        _truncate(text, max_chars or 0, hint="用 start_sec/end_sec 收窄时间窗"),
    )


@mcp.tool(structured_output=False)
def search_notes(
    query: str,
    scope: str = "all",
    task_id: Optional[str] = None,
    task_ids: Optional[list[str]] = None,
    top_k: int = 5,
    source: Literal["note", "transcript", "all"] = "note",
    include_synthetic: bool = False,
    section: Optional[str] = None,
    snippet_chars: int = 600,
) -> str:
    """语义检索笔记原文（回答笔记问题时的首选工具）。

    默认只返回笔记正文块（source="note"），过滤转写/AI 总结/目录，片段截到
    snippet_chars 字符；命中即可回答，不要为了确认再 list_notes。

    Args:
        query: 检索问题或关键词（支持中文自然语言）。
        scope: "all" 跨全部笔记（默认）；"current" 只查 task_id 指定的一篇；
               也可直接传单个 task_id。
        task_id: scope="current" 时必填。
        task_ids: 限定检索范围到这几篇笔记（可选）。
        top_k: 最终最多返回条数（1-30，默认 5）。
        source: "note" 只要笔记正文（默认）/ "transcript" 只要转写原片 / "all"。
        include_synthetic: 是否保留 "AI 总结"/"目录" 这类合成小节（默认 False）。
        section: 只保留小节标题匹配该关键词的结果（如 "对策"）。
        snippet_chars: 每个片段最多字符数（默认 600；0 表示不截断）。

    Returns:
        编号片段列表：来源笔记《标题》· 小节 · 相关度 + task_id + 原文片段。
    """
    try:
        top_k = max(1, min(int(top_k), 30))
    except (TypeError, ValueError):
        top_k = 5
    filtered_request = source != "all" or not include_synthetic or bool(section)
    request_k = min(30, top_k * 2) if filtered_request else top_k
    payload: dict = {"query": query, "scope": scope, "top_k": request_k}
    if task_id:
        payload["task_id"] = task_id
    if task_ids:
        payload["task_ids"] = task_ids
    data = _data("POST", "/api/chat/search", payload) or {}
    results = data.get("results", []) or []
    kept = [
        r
        for r in results
        if _search_keep(r, source, include_synthetic, section)
    ][:top_k]
    if not kept:
        return _ret("search_notes", _search_empty_message(query, source, top_k))

    filters = [f"source={source}"]
    if not include_synthetic:
        filters.append("已过滤 AI 总结/目录")
    if section:
        filters.append(f"section≈{section}")
    lines = [
        f"「{query}」检索到 {len(kept)} 个片段（本地向量，{'，'.join(filters)}）：\n"
    ]
    for i, r in enumerate(kept, 1):
        src = f"《{r.get('note_title') or '未知笔记'}》"
        if r.get("section_title"):
            src += f" · {r['section_title']}"
        stype = r.get("source_type") or "chunk"
        if stype == "transcript" and r.get("start_time") is not None:
            src += f" · 原片 {_fmt_ts(r.get('start_time'))}"
        dist = r.get("distance")
        dist_txt = f" · 相关度 {1 - dist:.2f}" if isinstance(dist, (int, float)) else ""
        tid = r.get("task_id") or ""
        text = _strip_images(r.get("text", "") or "")
        if snippet_chars and len(text) > snippet_chars:
            text = text[:snippet_chars].rstrip() + "…"
        lines.append(f"### {i}. {src}{dist_txt}\ntask_id: `{tid}`\n\n{text}\n")
    lines.append(
        "\n提示：引用时注明笔记来源；片段被截断或需要该节全文时，"
        "用 get_note(task_id=..., section=\"小节标题\")。"
    )
    return _ret("search_notes", "\n".join(lines))


@mcp.tool(structured_output=False)
def ask_notes(
    question: str,
    scope: str = "all",
    task_id: Optional[str] = None,
    task_ids: Optional[list[str]] = None,
    provider_id: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """【兜底】用 BiliNote 内置的在线 API 问答（RAG + 内置模型作答）。

    注意：这只是 BiliNote 自带的问答能力，通常不如 harness 自身模型强。
    默认场景请改用 search_notes 拿原文自己推理。仅当用户明确要求
    "用 BiliNote 自带问答"时才调用本工具。

    Args:
        question: 问题。
        scope: "all"（跨笔记，默认）或 "current"。
        task_id: scope="current" 时必填；scope="all" 缺省时自动取最近一篇。
        task_ids: 限定范围（可选）。
        provider_id / model_name: 不传时自动选第一个启用的供应商和模型。

    Returns:
        内置模型生成的答案 + 引用来源列表。
    """
    pid, mname = _resolve_provider_model(provider_id, model_name)
    if scope == "current" and not task_id:
        raise BiliNoteError("scope=current 时必须提供 task_id")
    if not task_id:
        tasks = [t for t in _recent_tasks() if t.get("status") == "SUCCESS"]
        if not tasks:
            raise BiliNoteError("笔记库中没有已完成的笔记，无法问答。")
        task_id = tasks[0]["task_id"]

    payload = {
        "task_id": task_id,
        "question": question,
        "history": [],
        "provider_id": pid,
        "model_name": mname,
        "scope": scope,
    }
    if task_ids:
        payload["task_ids"] = task_ids
    data = _data("POST", "/api/chat/ask", payload) or {}

    lines = [data.get("answer", "(空回答)"), "\n来源："]
    for s in data.get("sources", []) or []:
        title = s.get("note_title") or task_id[:8]
        loc = s.get("section_title") or ""
        if s.get("start_time") is not None:
            loc += f" {_fmt_ts(s.get('start_time'))}"
        stype = s.get("source_type") or ""
        lines.append(f"- 《{title}》 {loc} [{stype}]")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
def create_note(
    video_url: str,
    platform: Optional[str] = None,
    quality: str = "medium",
    model_name: Optional[str] = None,
    provider_id: Optional[str] = None,
    style: Optional[str] = None,
    screenshot: bool = False,
) -> str:
    """提交一个视频链接，让 BiliNote 生成笔记（异步任务）。

    Args:
        video_url: 视频链接（支持 bilibili / youtube / douyin / xiaohongshu，
                   B 站稍后再看列表页链接会批量导入）。
        platform: 平台；不传则从链接自动识别。
        quality: 下载质量 fast / medium / slow（默认 medium）。
        model_name / provider_id: 笔记生成用的模型；不传自动选第一个可用的。
        style: 笔记风格提示词（可选，遵循 BiliNote 设置中的默认风格）。
        screenshot: 是否截取关键帧图（可选）。

    Returns:
        task_id 与查询提示。任务排队+生成通常需要几分钟，用 get_note(task_id) 轮询。
    """
    pid, mname = _resolve_provider_model(provider_id, model_name)
    plat = platform or _detect_platform(video_url)
    if quality not in ("fast", "medium", "slow"):
        quality = "medium"

    payload: dict = {
        "video_url": video_url,
        "platform": plat,
        "quality": quality,
        "model_name": mname,
        "provider_id": pid,
        "screenshot": screenshot,
        "format": ["link"] + (["screenshot"] if screenshot else []),
    }
    if style:
        payload["style"] = style

    try:
        data = _data("POST", "/api/generate_note", payload) or {}
    except BiliNoteError as e:
        # 300102：本地转写模型未就绪（需在 BiliNote 设置里先下载 whisper 模型）
        if getattr(e, "code", None) == 300102:
            raise BiliNoteError(f"转写模型未就绪，无法生成笔记：{e}")
        dup = (getattr(e, "data", None) or {}).get("existing_task_id")
        if dup:
            return f"该视频已在生成队列中，复用任务 task_id=`{dup}`，用 get_note 轮询即可。"
        raise
    if data.get("batch"):
        created = data.get("created", []) or []
        lines = [f"稍后再看批量导入：已创建 {len(created)} 个任务。"]
        for it in created:
            lines.append(f"- {it.get('title') or it.get('video_id')} task_id=`{it.get('task_id')}`")
        lines.append("用 get_note(task_id) 逐个轮询。")
        return "\n".join(lines)

    task_id = data.get("task_id")
    return (
        f"已提交生成任务 task_id=`{task_id}`（平台 {plat}，模型 {mname}）。\n"
        "生成需要几分钟（下载→转写→总结），期间用 get_note(task_id) 轮询状态。"
    )


@mcp.tool(structured_output=False)
def cancel_task(task_id: str) -> str:
    """停止正在生成（或排队中）的笔记任务。

    用户改主意/参数设置错了时用：立即中止下载、转写重试、总结降级阶梯等
    一切进行中的步骤，状态落「已停止」（CANCELLED，不是失败），随后可用
    retry_task 换模型重新生成。

    Args:
        task_id: 要停止的任务 ID（list_notes 返回的 task_id）。

    Returns:
        停止受理确认；任务状态可用 get_note(task_id) 确认。
    """
    data = _data("POST", "/api/cancel_task", {"task_id": task_id})
    if not data.get("cancelled"):
        return f"任务 {task_id} 当前未在生成（{data.get('message') or data.get('status') or '已结束'}），无需停止。"
    return f"已停止任务 {task_id}。如需换模型重跑，用 retry_task 或重新 create_note。"


@mcp.tool(structured_output=False)
def retry_task(task_id: str) -> str:
    """重试一个失败的笔记生成任务（沿用原视频与原模型配置）。

    Args:
        task_id: 要重试的任务 ID。

    Returns:
        重试已受理的确认信息；继续用 get_note(task_id) 轮询。
    """
    task = next((t for t in _recent_tasks() if t.get("task_id") == task_id), None)
    if not task:
        raise BiliNoteError(f"找不到任务 {task_id}（可能已被删除），用 list_notes 确认。")
    if not task.get("video_url"):
        raise BiliNoteError(f"任务 {task_id} 缺少视频链接信息，无法自动重试。")

    payload = {
        "video_url": task["video_url"],
        "platform": task.get("platform") or _detect_platform(task["video_url"]),
        "quality": "medium",
        "model_name": task.get("model_name") or "",
        "provider_id": task.get("provider_id") or "",
        "task_id": task_id,
    }
    if not payload["model_name"] or not payload["provider_id"]:
        pid, mname = _resolve_provider_model(None, None)
        payload["model_name"] = payload["model_name"] or mname
        payload["provider_id"] = payload["provider_id"] or pid

    _data("POST", "/api/generate_note", payload)
    return f"任务 {task_id} 已重新提交，用 get_note(task_id) 轮询。"


@mcp.tool(structured_output=False)
def export_note(task_id: str, output_format: str = "pdf", save_dir: Optional[str] = None) -> str:
    """把一篇笔记导出为 PDF 或 Word 文档并保存到本机。

    Args:
        task_id: 笔记任务 ID。
        output_format: "pdf" 或 "docx"（默认 pdf）。
        save_dir: 保存目录（默认 %USERPROFILE%\\Downloads\\BiliNote）。

    Returns:
        导出文件的完整路径。
    """
    fmt = output_format.lower()
    if fmt not in ("pdf", "docx", "word"):
        raise BiliNoteError(f"不支持的导出格式：{output_format}（仅 pdf / docx）")
    fmt = "docx" if fmt == "word" else fmt

    data = _get_note_or_raise(task_id)
    result = data.get("result", {}) or {}
    md = _note_markdown(result)
    if not md:
        raise BiliNoteError(f"任务 {task_id} 没有可导出的笔记内容。")
    meta = result.get("audio_meta", {}) or {}
    title = meta.get("title") or f"note_{task_id[:8]}"

    save_dir = save_dir or os.path.join(
        os.environ.get("USERPROFILE", os.path.expanduser("~")), "Downloads", "BiliNote"
    )
    os.makedirs(save_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip() or task_id[:8]
    path = os.path.join(save_dir, f"{safe}_{task_id[:8]}.{fmt}")

    try:
        blob = _call(
            "POST",
            "/api/export_note",
            {"markdown": md, "title": title, "output_format": fmt},
        ).get("data")
    except BiliNoteError:
        raise
    if not blob:
        raise BiliNoteError("导出接口返回空内容。")
    with open(path, "wb") as f:
        f.write(blob)
    return f"已导出：{path}"


@mcp.tool(structured_output=False)
def delete_note(
    task_id: Optional[str] = None,
    video_id: Optional[str] = None,
    platform: Optional[str] = None,
    force: bool = False,
    confirm: bool = False,
) -> str:
    """【危险操作】删除笔记任务：笔记文件、向量索引、任务记录一并清除，不可恢复。

    必须在用户明确表达删除意图后才能调用；调用时传 confirm=true 表示已获用户确认。
    同一视频有多条任务时优先用 video_id+platform 全部清理，单条用 task_id。

    Args:
        task_id: 要删除的任务 ID（与 video_id 二选一）。
        video_id: 按视频 ID 删除（配合 platform）。
        platform: video_id 对应的平台（bilibili / youtube / ...）。
        force: 该视频正在生成中时是否强行删除（默认 false）。
        confirm: 必须为 true 才会执行删除。

    Returns:
        删除结果确认。
    """
    if not confirm:
        return (
            "删除是不可恢复操作（笔记 markdown、转写、向量索引、任务记录全部清除）。\n"
            "请先向用户确认要删除哪条笔记，然后带 confirm=true 重新调用。"
        )
    if not task_id and not video_id:
        raise BiliNoteError("必须提供 task_id 或 video_id+platform 之一。")

    payload: dict = {"force": force}
    if task_id:
        payload["task_id"] = task_id
    else:
        payload["video_id"] = video_id
        payload["platform"] = platform or ""
    _data("POST", "/api/delete_task", payload)
    target = task_id or f"{video_id}({platform})"
    return f"已删除笔记任务：{target}。"


@mcp.tool(structured_output=False)
def index_status() -> str:
    """查看笔记库健康状态：后端是否在线、向量索引覆盖率、缺失索引的笔记清单。

    Returns:
        后端状态 + 已索引/总笔记数 + 未索引清单（这些笔记检索不到，
        可用 reindex_notes 补建）。
    """
    try:
        # sys_check 信封 data 为 None，能走到这里就代表后端在线
        _data("GET", "/api/sys_check")
        health_txt = "在线"
    except BiliNoteError as e:
        health_txt = f"异常（{e}）"
    coverage = _data("GET", "/api/chat/coverage") or {}
    indexed = coverage.get("indexed", 0)
    total = coverage.get("total_notes", 0)
    missing = coverage.get("missing", []) or []

    lines = [
        f"后端：{health_txt}（{BASE_URL}）",
        f"向量索引覆盖：{indexed}/{total} 篇",
    ]
    if missing:
        show = missing[:20]
        lines.append(f"未索引 {len(missing)} 篇（检索不到，建议 reindex_notes 补建）：")
        lines.extend(f"- `{t}`" for t in show)
        if len(missing) > len(show):
            lines.append(f"- …其余 {len(missing) - len(show)} 篇略")
    else:
        lines.append("全部笔记均已索引。")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
def reindex_notes(task_ids: Optional[list[str]] = None) -> str:
    """为缺失索引的笔记补建向量索引（后台执行）。

    Args:
        task_ids: 只补建这几篇；不传则自动补建全部缺失的（后台逐篇执行）。

    Returns:
        受理确认。索引进度用 index_status 查看；每篇索引需数秒，
        大库全量补建可能要几分钟。
    """
    payload = {"task_ids": task_ids} if task_ids else {}
    _data("POST", "/api/chat/backfill", payload)
    scope = f"{len(task_ids)} 篇" if task_ids else "全部缺失笔记"
    return f"已开始后台补建索引（{scope}），用 index_status 查看进度。"


def main():
    try:
        mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

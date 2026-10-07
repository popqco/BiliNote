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
from typing import Optional

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
        "BiliNote 视频笔记库（B站/YouTube 等视频的 AI 笔记）。"
        "回答与笔记内容相关的问题时：优先 search_notes 检索原文块 → 需要更多上下文再 "
        "get_note / get_transcript，用你自己的能力推理作答并注明出自哪篇笔记；"
        "ask_notes 是软件内置的在线 API 问答，仅在用户明确要求时作兜底使用。"
    ),
)


class BiliNoteError(Exception):
    """带业务语义的错误：msg 面向 harness 可读。"""


def _client() -> httpx.Client:
    headers = {}
    if TOKEN:
        headers["X-Pairing-Token"] = TOKEN
    return httpx.Client(base_url=BASE_URL, timeout=_TIMEOUT, headers=headers)


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


@mcp.tool()
def list_notes(limit: int = 30, status: str = "all") -> str:
    """列出 BiliNote 笔记库中的笔记任务（按最近更新排序）。

    Args:
        limit: 最多返回条数（1-300，默认 30）。
        status: 过滤状态：all / SUCCESS / FAILED 或进行中状态（默认 all）。

    Returns:
        每行一条：标题、平台、状态、更新时间、task_id、视频链接。
        task_id 用于后续 get_note / search_notes(scope=current) / export_note 等调用。
    """
    tasks = _recent_tasks(limit=300)
    if status and status != "all":
        tasks = [t for t in tasks if t.get("status") == status]
    tasks = tasks[: max(1, min(limit, 300))]
    if not tasks:
        return "笔记库为空（或没有匹配状态的笔记）。"

    lines = []
    for t in tasks:
        title = t.get("title") or "(无标题)"
        lines.append(
            f"- {title} | {t.get('platform', '-')} | {t.get('status')} "
            f"| {t.get('updated_at', '')} | task_id=`{t.get('task_id')}` "
            f"| {t.get('video_url', '')}"
        )
    header = f"共 {len(tasks)} 条笔记："
    if any(t.get("status") == "FAILED" for t in tasks):
        header += "（含失败任务，可用 retry_task 重试）"
    return header + "\n" + "\n".join(lines)


@mcp.tool()
def get_note(task_id: str) -> str:
    """读取一篇笔记的完整 markdown 正文与元数据；也可用于查询任务状态。

    Args:
        task_id: 笔记任务 ID（list_notes 返回的 task_id）。

    Returns:
        SUCCESS：元数据（标题/平台/时长/链接）+ 完整 markdown 笔记正文。
        未完成/失败：当前状态、进度消息（可据此轮询或 retry_task）。
    """
    data = _get_note_or_raise(task_id)
    status = data.get("status", "UNKNOWN")

    if status != "SUCCESS":
        return (
            f"任务 {task_id} 尚未完成：status={status}，"
            f"message={data.get('message') or '-'}。"
            "进行中可稍后重试本工具轮询；FAILED 可用 retry_task 重试。"
        )

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
    return header + md


@mcp.tool()
def get_transcript(
    task_id: str, start_sec: float = 0, end_sec: Optional[float] = None
) -> str:
    """读取一篇笔记对应的视频转写文本（按时间排序的分段原话）。

    Args:
        task_id: 笔记任务 ID。
        start_sec: 起始秒（默认 0）。
        end_sec: 结束秒（默认到结尾）。

    Returns:
        `[mm:ss] 原话` 逐行文本。适合核对视频里的原始表述、引用原话。
    """
    data = _get_note_or_raise(task_id)
    result = data.get("result", {}) or {}
    transcript = result.get("transcript", {}) or {}
    segments = transcript.get("segments", []) or []
    if not segments:
        return f"任务 {task_id} 没有可用的转写内容。"

    out = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        if start < float(start_sec):
            continue
        if end_sec is not None and start > float(end_sec):
            continue
        out.append(f"[{_fmt_ts(start)}] {seg.get('text', '')}")
    if not out:
        return f"时间窗 [{start_sec}, {end_sec}] 内没有转写片段。"
    if len(out) > 500:
        out = out[:500] + [f"…（超出 500 行已截断，共 {len(out)} 行，可缩小时间窗）"]
    lang = transcript.get("language") or ""
    header = f"转写（{lang or '语言未知'}，{len(out)} 段）：\n"
    return header + "\n".join(out)


@mcp.tool()
def search_notes(
    query: str,
    scope: str = "all",
    task_id: Optional[str] = None,
    task_ids: Optional[list[str]] = None,
    top_k: int = 8,
) -> str:
    """语义检索笔记库，返回原文片段（这是回答笔记相关问题时的首选工具）。

    纯本地向量检索（chromadb），不调用任何在线 API、不消耗 token。
    片段按相关性排序，带来源（笔记标题/小节/时间戳），可直接作为引用依据。
    检索结果偏少时：换更具体的关键词、调大 top_k（≤30），或改用
    get_note 读整篇。需要某篇笔记的完整内容时不要反复 search，直接 get_note。

    Args:
        query: 检索问题或关键词（支持中文自然语言）。
        scope: "all" 跨全部笔记（默认）；"current" 只查 task_id 指定的一篇；
               也可直接传单个 task_id。
        task_id: scope="current" 时必填。
        task_ids: 限定检索范围到这几篇笔记（可选）。
        top_k: 最多返回条数（1-30，默认 8；实际可能因相关性闸门少于该值）。

    Returns:
        编号片段列表：来源笔记《标题》· 小节 · [时间] + 原文块 + 距离。
    """
    payload: dict = {"query": query, "scope": scope, "top_k": top_k}
    if task_id:
        payload["task_id"] = task_id
    if task_ids:
        payload["task_ids"] = task_ids
    data = _data("POST", "/api/chat/search", payload) or {}
    results = data.get("results", []) or []
    if not results:
        return (
            f"「{query}」没有检索到相关片段。可能原因：关键词太生僻（换表述）、"
            "笔记未建索引（用 index_status 查看，reindex_notes 补建）、或笔记库为空。"
        )

    lines = [f"「{query}」检索到 {len(results)} 个片段（本地向量检索，未调用在线 API）：\n"]
    for i, r in enumerate(results, 1):
        src = f"《{r.get('note_title') or '未知笔记'}》"
        if r.get("section_title"):
            src += f" · {r['section_title']}"
        stype = r.get("source_type") or "chunk"
        if stype == "transcript" and r.get("start_time") is not None:
            src += f" · 原片 {_fmt_ts(r.get('start_time'))}"
        dist = r.get("distance")
        dist_txt = f"（相关度 {1 - dist:.2f}）" if isinstance(dist, (int, float)) else ""
        tid = r.get("task_id") or ""
        lines.append(f"### {i}. {src} {dist_txt}\ntask_id: `{tid}`\n\n{r.get('text', '')}\n")
    lines.append(
        "\n提示：引用时注明出自哪篇笔记；片段不够用时可用 get_note(task_id) 读整篇，"
        "或 get_transcript(task_id) 看视频原话。"
    )
    return "\n".join(lines)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

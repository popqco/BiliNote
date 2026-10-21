"""笔记正文质量闸门。

背景（2026-10-21 实拍）：上游网关对政治敏感内容会返回 HTTP 200 + 一句
「抱歉，这个问题未找到相关结果。」，有时还会再套一段「AI 总结」。原流程
只检查 ``note.markdown`` 非空，于是把这种拒答当成成功笔记落盘，前端显示
「已完成」但正文没有任何有效信息。

这里在「模型返回」与「状态写 SUCCESS / 落盘」之间加一道确定性校验：

- 拒答/内容审核占位文本 → 致命错误，直接终止降级阶梯（换更小的请求重发
  同一段敏感内容没有意义），任务落 FAILED 并给出可操作的原因；
- 只有目录/AI 总结/来源链接、正文过短 → 非致命错误，交给上层降级阶梯再试
  一次（可能是截断/推理预算耗尽），仍失败则落 FAILED；
- 正常长文笔记不误伤：按“去掉来源链接、目录、AI 总结后的有效字符数”判断，
  并与转写长度做量级对照。

阈值特意保守：宁可在极端情况下把一个很短的合法笔记判失败，也不要把空壳
笔记标成完成——后者用户无法从 UI 分辨，危害更大。
"""
import re
from typing import Optional

# 来源链接是系统追加的元信息，不算正文
_SOURCE_LINK_RE = re.compile(r"^\s*>\s*来源链接[:：].*$", re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# 有效字符：字母/数字/汉字/假名/谚文。标点、Markdown 符号、空白都不算。
_SUBSTANTIVE_RE = re.compile(r"[\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

# 上游拒答/审核的典型占位文本。只在正文开头附近命中且正文很短时判死，
# 避免误伤“讨论搜索/审核机制”的合法笔记。
_REFUSAL_MARKERS = (
    "这个问题未找到相关结果",
    "未找到相关结果",
    "没有可提取的实质内容",
    "无法提供该",
    "无法回答该",
    "无法协助",
    "content_filter",
    "i'm sorry, but i can't",
    "i cannot assist",
    "unable to assist",
)
_REFUSAL_RE = re.compile(
    r"(?:抱歉|对不起)[，,]\s*(?:我)?(?:无法|不能|没有办法|没有|未找到)",
    re.IGNORECASE,
)

# 「AI 总结」是整篇级段落，目录/总结都不能当作正文
_GLOBAL_SECTIONS = {"目录", "ai总结", "aisummary", "ai摘要"}


class NoteContentError(RuntimeError):
    """模型返回的笔记正文不可用。

    ``fatal=True``：内容层面的拒答/审核，重试同一模型同一内容无意义，
    应直接终止降级阶梯；``fatal=False``：空/过短，可能是截断，允许再试。
    """

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _strip_global_sections(markdown: str) -> str:
    """去掉来源链接、目录、AI 总结与 HTML 注释，只留正文。"""
    text = _SOURCE_LINK_RE.sub("", markdown or "")
    text = _HTML_COMMENT_RE.sub("", text)

    out = []
    skip_level: Optional[int] = None
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if skip_level is None:
                out.append(line)
            continue
        if in_fence:
            if skip_level is None:
                out.append(line)
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = (
                m.group(2).strip().lower()
                .replace(" ", "")
                .replace("：", "")
                .replace(":", "")
            )
            if title in _GLOBAL_SECTIONS:
                skip_level = level if skip_level is None else min(skip_level, level)
                continue
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is not None:
                continue
        elif skip_level is not None:
            continue
        out.append(line)
    return "\n".join(out)


def _substantive_chars(text: str) -> int:
    return len(_SUBSTANTIVE_RE.findall(text or ""))


def _looks_like_refusal(body: str) -> bool:
    head = (body or "").strip()[:400].lower()
    if not head:
        return False
    if any(marker in head for marker in _REFUSAL_MARKERS):
        return True
    return bool(_REFUSAL_RE.search(head))


def ensure_note_content(markdown: str | None, transcript_text: str = "") -> None:
    """校验笔记正文；不可用时抛出 :class:`NoteContentError`。

    :param markdown: 模型返回并经过最终整理的 Markdown
    :param transcript_text: 原始转写文本，用于判断“长视频只回了几个字”的异常
    """
    full = (markdown or "").strip()
    if not full:
        raise NoteContentError("模型返回空内容，未生成有效笔记", fatal=False)

    body = _strip_global_sections(full)
    body_chars = _substantive_chars(body)
    transcript_chars = _substantive_chars(transcript_text)

    if _looks_like_refusal(body) and body_chars < 500:
        raise NoteContentError(
            "上游模型拒答或触发内容审核（返回了“未找到相关结果”之类的占位文本），"
            "未生成有效笔记；请更换模型后重试",
            fatal=True,
        )

    # 短视频（转写很短）允许更短的笔记；长视频只回几十个字基本可以确定是空壳。
    min_body = 120 if transcript_chars >= 1000 else 40
    if body_chars < min_body:
        raise NoteContentError(
            f"模型返回的正文有效内容过少（{body_chars} 字），已拦截，"
            "避免把空笔记标记为完成",
            fatal=False,
        )

    # 长转写、没有二级以下标题、正文又极短：即使没命中拒答词也按空壳处理。
    has_body_heading = bool(re.search(r"^#{2,6}\s+\S", body, re.M))
    if transcript_chars >= 5000 and body_chars < 300 and not has_body_heading:
        raise NoteContentError(
            f"模型返回的正文缺少可用的结构化内容（{body_chars} 字），已拦截",
            fatal=False,
        )

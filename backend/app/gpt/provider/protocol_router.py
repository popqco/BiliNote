"""协议自动纠错路由：让同一 base_url 下混装的 chat / responses 模型都能自愈。

背景（2026-10-17 实锤）：聚合网关（cmdc-pak / OpenCode Free 等）在同一个
base_url 下同时提供两类模型——只会 Chat Completions 的（space-bunny-free、
exo-free…）和只会 Responses 的（muse-spark-1.3-contributor-free）。BiliNote
的协议（api_format）是**供应商级**配置，而设置页「添加模型」的下拉取自网关
`/v1/models` 全量清单、不区分协议；模型一旦挂在协议不匹配的供应商下，每次
调用都被网关毫秒级 400 拒绝，且总结的四个降级策略全部原样重发，整条笔记必然
失败——表现为「同一个模型有时能生成、有时全灭」，取决于它挂在哪个供应商下：

  - chat 模型走 /v1/responses         →「不支持 Responses 协议，请改用 Chat Completions」
  - responses 模型走 /v1/chat/completions →「不支持 Chat Completions 协议…请改用 Responses」

这一层在客户端做三件事：
  1. 按「记忆 → 供应商配置」的顺序选首选协议；
  2. 首选协议被明确以「协议不匹配」拒绝时，就地换另一种协议重发一次
     （网关拒绝耗时毫秒级，实测 2.5ms，代价可忽略）；
  3. 把 (base_url, model) → 正确协议 记到 config/protocol_overrides.json，
     之后（含重启后）直接命中正确协议；文件缺失/损坏时按「没有记忆」继续。

只对「4xx + 文案明确指向协议」的错误做切换，其余错误（429/5xx/超时/空回等）
原样抛给上层重试与降级阶梯——502 这类上游抽风不能当成协议结论（实测
muse-spark 走正确的 /responses 也会 502「unexpected EOF」）。
"""

import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

OVERRIDES_PATH = Path(os.getenv("PROTOCOL_OVERRIDES_PATH", "config/protocol_overrides.json"))

_PROTOCOLS = ("chat", "responses")

# 网关拒绝文案 → 目标协议。中文是 cmdc-pak / OpenCode Free 的原文，
# 英文兜底其他兼容网关；只匹配「协议名 + 不支持/改用」的组合。
_SWITCH_TO_CHAT = re.compile(
    r"不支持\s*responses|改用\s*chat\s*completions|not\s+support\w*\s+(?:the\s+)?responses",
    re.IGNORECASE,
)
_SWITCH_TO_RESPONSES = re.compile(
    r"不支持\s*chat\s*completions|改用\s*responses|not\s+support\w*\s+(?:the\s+)?chat\s*completions",
    re.IGNORECASE,
)

# 协议结论只认这几种状态码：协议不匹配是确定性 4xx。
# 5xx（502 上游异常等）/超时/限流一律不属于此类。
_PROTOCOL_ERROR_STATUS = {400, 404, 405, 422, 501}


# 流式内容静默预算（秒），按链路区分（UniversalGPT 读取，env 仍可覆盖）：
#   responses：网关普遍把推理内容藏起来（cmdc-pak 思考期只剩 : ping 保活，
#   SDK 丢弃注释），内容静默 = 正在思考，给足预算，别误判假死触发减帧降级；
#   chat：推理流式可见，静默即异常，保持快速失败。
_STALL_BY_PROTOCOL = {"chat": 120.0, "responses": 600.0}


def detect_protocol_switch(exc: Exception, current: str) -> Optional[str]:
    """错误是否明确要求换协议；返回目标协议（chat / responses）或 None。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        status_int = int(status) if status is not None else None
    except Exception:
        status_int = None
    if status_int is not None and status_int not in _PROTOCOL_ERROR_STATUS:
        return None

    raw = str(exc)
    # 两个方向都判、且排除「当前协议自己」：文案里同时出现另一种协议名
    # 属于正常（“请改用 X”），不会误判（见模块 docstring 的两条原文）。
    if current != "responses" and _SWITCH_TO_RESPONSES.search(raw):
        return "responses"
    if current != "chat" and _SWITCH_TO_CHAT.search(raw):
        return "chat"
    return None


class _OverrideStore:
    """(base_url, model) → 协议 的小型 JSON 记忆：进程内缓存 + 原子落盘。"""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._data: Optional[dict] = None

    def _load_locked(self) -> dict:
        if self._data is None:
            data = {}
            if self._path.exists():
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"协议记忆文件不可读，按空处理（{e}）：{self._path}")
                    data = {}
            if not isinstance(data, dict):
                data = {}
            self._data = data
        return self._data

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            value = self._load_locked().get(key)
            return value if value in _PROTOCOLS else None

    def set(self, key: str, protocol: str) -> None:
        if protocol not in _PROTOCOLS:
            return
        with self._lock:
            data = self._load_locked()
            if data.get(key) == protocol:
                return
            data[key] = protocol
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
            except Exception as e:
                # 写不进去只影响「下次还要再纠一遍」，不影响本次调用
                logger.warning(f"协议记忆写入失败（不影响本次调用）：{e}")


_STORE = _OverrideStore(OVERRIDES_PATH)


def _key(base_url: str, model: str) -> str:
    return f"{(base_url or '').rstrip('/')}|{model or ''}"


def get_override(base_url: str, model: str) -> Optional[str]:
    return _STORE.get(_key(base_url, model))


def set_override(base_url: str, model: str, protocol: str) -> None:
    _STORE.set(_key(base_url, model), protocol)


class _Completions:
    """伪 client.chat.completions：按协议路由 create()。"""

    def __init__(self, owner: "AutoProtocolClient"):
        self._owner = owner

    def create(self, model=None, messages=None, stream=False, **kwargs):
        return self._owner._dispatch(model=model, messages=messages, stream=stream, kwargs=kwargs)


class AutoProtocolClient:
    """包一层 OpenAI SDK client：.chat.completions 按 (记忆 | 首选) 协议路由。

    首选协议 = 记忆里的选择 → 供应商配置的 api_format。首选被明确拒绝为
    「协议不匹配」时就地换另一种重发并记住；其余异常原样上抛。其余属性
    （.models、.base_url…）原样透传，模型列表拉取不受影响。
    """

    def __init__(self, inner, preferred: str = "chat"):
        # 只用 object.__setattr__，避免 __getattr__ 递归
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_preferred", preferred if preferred in _PROTOCOLS else "chat")
        object.__setattr__(self, "_base", str(getattr(inner, "base_url", "") or ""))
        object.__setattr__(self, "_active_protocol", None)

    @property
    def stream_stall_seconds(self) -> float:
        """当前链路的流式静默预算（UniversalGPT 的假死闸读取）。"""
        protocol = self._active_protocol or self._preferred
        return _STALL_BY_PROTOCOL.get(protocol, 120.0)

    @property
    def chat(self):
        return SimpleNamespace(completions=_Completions(self))

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ---------------- 内部 ----------------

    def _order(self, model: str):
        first = get_override(self._base, model) or self._preferred
        second = "responses" if first == "chat" else "chat"
        return [first, second]

    def _open(self, protocol: str, model, messages, stream: bool, kwargs):
        # 记录本次实际使用的协议：流式静默预算按它取值（思考期是否可见）
        object.__setattr__(self, "_active_protocol", protocol)
        if protocol == "responses":
            from app.gpt.provider.responses_compat import ResponsesCompatClient

            client = ResponsesCompatClient(self._inner)
        else:
            client = self._inner
        return client.chat.completions.create(
            model=model, messages=messages, stream=stream, **kwargs
        )

    def _learn(self, idx: int, protocol: str, exc: Exception, model: str, order: list) -> bool:
        """协议不匹配时记住并返回 True（可以换协议重发）；否则 False。"""
        if idx + 1 >= len(order):
            return False
        target = detect_protocol_switch(exc, protocol)
        if not target:
            return False
        logger.warning(
            f"[protocol_router] {self._base} 的模型 {model} 拒绝 {protocol} 协议调用"
            f"（{str(exc)[:160]}），自动改用 {target} 重发并记住该选择"
        )
        set_override(self._base, model, target)
        return True

    def _dispatch(self, model, messages, stream: bool, kwargs):
        model = model or ""
        order = self._order(model)
        if stream:
            return self._stream(order, model, messages, kwargs)

        last_exc = None
        for idx, protocol in enumerate(order):
            try:
                return self._open(protocol, model, messages, False, kwargs)
            except Exception as exc:
                last_exc = exc
                if not self._learn(idx, protocol, exc, model, order):
                    raise
        raise last_exc if last_exc is not None else RuntimeError("protocol router: unreachable")

    def _stream(self, order, model, messages, kwargs):
        """流式：协议拒绝可能在 create() 当场抛出（chat 直连），也可能在
        首次迭代时抛出（responses 翻译层是惰性生成器）——两种都要能换协议
        重放，前提是还一个 chunk 都没吐给调用方（吐过了重放会重复内容，
        交给上层重试）。"""
        for idx, protocol in enumerate(order):
            yielded = False
            stream_obj = None
            try:
                stream_obj = self._open(protocol, model, messages, True, kwargs)
                for chunk in stream_obj:
                    yielded = True
                    yield chunk
                return
            except Exception as exc:
                if stream_obj is not None:
                    try:
                        stream_obj.close()
                    except Exception:
                        pass
                if yielded or not self._learn(idx, protocol, exc, model, order):
                    raise
        return

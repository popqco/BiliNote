"""协议自动纠错路由 + Responses 链路假死判定 的验证脚本（2026-10-17）。

用法（backend 目录下，用带依赖的 venv 跑）：
    python scripts/check_protocol_router.py            # 单元 + 网关实测
    python scripts/check_protocol_router.py --offline  # 只跑单元测试

单元部分不联网；实测部分打 127.0.0.1:8787（cmdc-pak 反代），
故意把模型配到错误的协议上，验证「自动换协议 + 记住」。
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 测试用的协议记忆文件：别碰真实 config/
_TEST_OVERRIDES = Path(tempfile.gettempdir()) / "bilinote_protocol_overrides_test.json"
if _TEST_OVERRIDES.exists():
    _TEST_OVERRIDES.unlink()
os.environ["PROTOCOL_OVERRIDES_PATH"] = str(_TEST_OVERRIDES)
os.environ.pop("OPENAI_STREAM_STALL_SECONDS", None)
os.environ["NOTE_OUTPUT_DIR"] = str(Path(tempfile.gettempdir()) / "bilinote_check_note_results")

from app.gpt.provider import protocol_router as pr  # noqa: E402
from app.gpt.provider.responses_compat import ResponsesCompatClient  # noqa: E402
from app.gpt.universal_gpt import UniversalGPT, CompletionStalled  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------- 单元测试
def test_detect_switch():
    print("\n== detect_protocol_switch ==")
    exc400 = lambda msg: type("E", (Exception,), {"status_code": 400})(msg)  # noqa: E731
    exc502 = lambda msg: type("E", (Exception,), {"status_code": 502})(msg)  # noqa: E731

    chat_only = ('Error code: 400 - {\'error\': {\'code\': 400, \'message\': '
                 '\'模型 space-bunny-free 不支持 Responses 协议，请改用 Chat Completions（/v1/chat/completions）\'}}')
    resp_only = ('Error code: 400 - {\'error\': {\'code\': 400, \'message\': '
                 '\'模型 muse-spark-1.3-contributor-free 不支持 Chat Completions 协议（本线不做协议转换），'
                 '请改用 Responses（端点 /v1/responses）\'}}')
    upstream_502 = ('Error code: 502 - {\'error\': {\'code\': 502, \'message\': '
                    '\'OpenCode Free 上游异常：unexpected EOF\'}}')

    check("chat-only 文案（当前 responses）→ 切换 chat",
          pr.detect_protocol_switch(exc400(chat_only), "responses") == "chat")
    check("responses-only 文案（当前 chat）→ 切换 responses",
          pr.detect_protocol_switch(exc400(resp_only), "chat") == "responses")
    check("chat-only 文案但已在 chat → 不动",
          pr.detect_protocol_switch(exc400(chat_only), "chat") is None)
    check("上游 502 不算协议结论",
          pr.detect_protocol_switch(exc502(upstream_502), "chat") is None)
    check("无关 400（reasoning_effort）不算协议结论",
          pr.detect_protocol_switch(exc400("Unsupported value: 'reasoning_effort'"), "chat") is None)


def test_translator_keepalive():
    print("\n== Responses 翻译层：结构性事件透传为 keepalive ==")

    class FakeResponses:
        def create(self, **body):
            events = [
                SimpleNamespace(type="response.created"),
                SimpleNamespace(type="response.in_progress"),
                SimpleNamespace(type="response.output_item.added"),
                SimpleNamespace(type="response.output_text.delta", delta="你"),
                SimpleNamespace(type="response.output_text.delta", delta="好"),
                SimpleNamespace(type="response.completed"),
            ]
            return iter(events)

    fake = SimpleNamespace(responses=FakeResponses(), base_url="http://fake/v1")
    chunks = list(
        ResponsesCompatClient(fake).chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}], stream=True
        )
    )
    keepalive = sum(1 for c in chunks if getattr(c.choices[0].delta, "keepalive", False))
    text = "".join(c.choices[0].delta.content or "" for c in chunks)
    check("结构事件转成 keepalive 增量", keepalive == 3, f"keepalive={keepalive}")
    check("正文增量不受影响", text == "你好", f"text={text!r}")


def test_stall_gate():
    print("\n== 假死闸：keepalive 算活跃；真静默仍超时 ==")

    def make_client(chunks_fn):
        gpt = UniversalGPT(client=SimpleNamespace(), model="fake")
        gpt.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: chunks_fn()))
        )
        return gpt

    def keepalive_then_text():
        # 0.2s 一个保活增量，共约 1.2s（静默上限设 0.5s）：不应假死
        for _ in range(6):
            time.sleep(0.2)
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, keepalive=True), finish_reason=None)])
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="done", keepalive=False), finish_reason="stop")])

    def silent_then_text():
        # 先静默 1.0s（静默上限 0.5s）再吐正文：应判假死
        time.sleep(1.0)
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="late", keepalive=False), finish_reason=None)])

    os.environ["OPENAI_STREAM_STALL_SECONDS"] = "0.5"
    gpt = make_client(keepalive_then_text)
    try:
        out = gpt._chat_completion_create([{"role": "user", "content": "x"}])
        check("保活增量让长思考不被误杀", out.choices[0].message.content == "done")
    except CompletionStalled as e:
        check("保活增量让长思考不被误杀", False, str(e))

    gpt = make_client(silent_then_text)
    try:
        gpt._chat_completion_create([{"role": "user", "content": "x"}])
        check("真·内容静默仍然超时", False, "没有抛 CompletionStalled")
    except CompletionStalled:
        check("真·内容静默仍然超时", True)
    os.environ.pop("OPENAI_STREAM_STALL_SECONDS", None)


def test_route_stall_budget():
    print("\n== 链路静默预算 ==")
    fake = SimpleNamespace(base_url="http://127.0.0.1:8787/v1")
    check("chat 链路 120s",
          pr.AutoProtocolClient(fake, preferred="chat").stream_stall_seconds == 120.0)
    check("responses 链路 600s",
          pr.AutoProtocolClient(fake, preferred="responses").stream_stall_seconds == 600.0)


# ---------------------------------------------------------------- 网关实测
def test_live_router():
    print("\n== 网关实测（127.0.0.1:8787，故意配错协议）==")
    from app.gpt.gpt_factory import GPTFactory
    from app.models.model_config import ModelConfig

    def build(model_name: str, api_format: str) -> UniversalGPT:
        return GPTFactory.from_config(ModelConfig(
            name="test", provider="custom", api_key="local-proxy",
            base_url="http://127.0.0.1:8787/v1",
            model_name=model_name, api_format=api_format, thinking_effort="",
        ))

    # 1) chat-only 模型配到 responses：应自动换 chat 成功，并记住
    gpt = build("space-bunny-free", "responses")
    try:
        out = gpt._chat_completion_create(
            [{"role": "user", "content": "只回答两个字：好的"}])
        text = out.choices[0].message.content or ""
        check("space-bunny-free 配错 responses 仍成功（自动换 chat）", bool(text.strip()),
              f"回答={text.strip()[:40]!r}")
    except Exception as e:
        check("space-bunny-free 配错 responses 仍成功（自动换 chat）", False, repr(e))
    check("已记住 space-bunny-free→chat",
          pr.get_override("http://127.0.0.1:8787/v1", "space-bunny-free") == "chat")

    # 2) 记忆生效：现在首选协议应直接是 chat
    gpt2 = build("space-bunny-free", "responses")
    check("重启/重建后直接命中记忆（chat）",
          gpt2.client._order("space-bunny-free")[0] == "chat")

    # 2b) 非流式路径（AI 问答用的 chat 形态）也要能自愈：换一个没记忆的
    #     chat-only 模型 exo-free，同样故意配到 responses
    gpt_ns = build("exo-free", "responses")
    try:
        resp = gpt_ns.client.chat.completions.create(
            model="exo-free",
            messages=[{"role": "user", "content": "只回答两个字：好的"}],
            max_tokens=64,
        )
        text = resp.choices[0].message.content or ""
        check("非流式：exo-free 配错 responses 仍成功（自动换 chat）", bool(text.strip()),
              f"回答={text.strip()[:40]!r}")
    except Exception as e:
        check("非流式：exo-free 配错 responses 仍成功（自动换 chat）", False, repr(e))

    # 3) responses-only 模型配到 chat：应自动换 responses（上游偶发 502，容忍）
    gpt3 = build("muse-spark-1.3-contributor-free", "chat")
    try:
        out = gpt3._chat_completion_create(
            [{"role": "user", "content": "只回答两个字：好的"}])
        text = out.choices[0].message.content or ""
        check("muse-spark 配错 chat 仍成功（自动换 responses）", bool(text.strip()),
              f"回答={text.strip()[:40]!r}")
    except Exception as e:
        # 换了协议但上游抽风（502/静默）也算路由正确，记忆文件可证
        learned = pr.get_override(
            "http://127.0.0.1:8787/v1", "muse-spark-1.3-contributor-free") == "responses"
        check("muse-spark 配错 chat：路由已切换（上游抽风可容忍）", learned, repr(e))
    check("已记住 muse-spark→responses",
          pr.get_override("http://127.0.0.1:8787/v1", "muse-spark-1.3-contributor-free") == "responses")

    print(f"\n  协议记忆文件内容：{_TEST_OVERRIDES.read_text(encoding='utf-8') if _TEST_OVERRIDES.exists() else '(无)'}")


if __name__ == "__main__":
    test_detect_switch()
    test_translator_keepalive()
    test_stall_gate()
    test_route_stall_budget()
    if "--offline" not in sys.argv:
        test_live_router()
    print("\n" + ("全部通过 ✅" if not FAILURES else f"失败 {len(FAILURES)} 项 ❌: {FAILURES}"))
    sys.exit(1 if FAILURES else 0)

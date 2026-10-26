# BiliNote MCP Server —— 把笔记库接入外部 AI Harness（Codex / ZCode 等）

BiliNote 可以作为一个 **MCP（Model Context Protocol）服务器**，让 Codex、ZCode 等
外部 AI 工具直接读取你的笔记库并调用核心功能。

设计哲学：**harness 优先**。检索走纯本地向量召回（`search_notes`，零在线 API、零
token 消耗），原始片段交给 harness 自己的强模型推理作答——通常远好于 BiliNote 内置
的在线 API 问答；内置问答（`ask_notes`）保留为兜底。

## 前置条件

- BiliNote 桌面端**正在运行**（后端 `BiliNoteBackend` 随桌面端启停；桌面端没开时
  MCP 工具会返回带指引的报错）。
- 一个含 `mcp` 包的 Python 环境（共享 venv 已装，见 `backend/requirements.txt` 的
  `mcp>=1.9,<2`）。
- 服务器脚本：`backend/mcp_server.py`。

本机访问**无需任何鉴权**（后端对 127.0.0.1 回环免配对 token）。

## 接入 ZCode

`~/.zcode/cli/config.json` 的 `mcp.servers` 中加：

```json
"bilinote": {
  "type": "stdio",
  "command": "C:\\path\\to\\venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\BiliNote\\backend\\mcp_server.py"],
  "env": { "BILINOTE_BASE_URL": "http://127.0.0.1:8483" },
  "timeoutMs": 180000
}
```

重启 ZCode 即自动连接（Settings → MCP 可看状态）。

## 接入 Codex

`~/.codex/config.toml` 追加：

```toml
[mcp_servers.bilinote]
command = 'C:\path\to\venv\Scripts\python.exe'
args = ['C:\path\to\BiliNote\backend\mcp_server.py']
startup_timeout_sec = 60
tool_timeout_sec = 180

[mcp_servers.bilinote.env]
BILINOTE_BASE_URL = "http://127.0.0.1:8483"
```

## 远程 Worker（Tailscale / 局域网）

MCP 服务器跑在另一台机器、连远程 Worker 时，填 Worker 地址 + 配对 token
（桌面端「设置 → 连接」可复制）：

```json
"env": {
  "BILINOTE_BASE_URL": "http://100.x.y.z:8483",
  "BILINOTE_TOKEN": "<配对 token>"
}
```

远控开关关闭时，远程只放行任务类操作白名单（列笔记/读笔记/检索/问答/提交/重试等），
删除与配置类操作仍需在 Worker 本机执行。

## 工具清单（13 个）

| 工具 | 作用 | 类型 |
|---|---|---|
| `search_notes` | **首选**：语义检索原文块；默认只回笔记正文（`source="note"`），过滤转写/AI 总结/目录，片段截到 `snippet_chars` | 本地向量，零在线 API |
| `note_outline` | 看笔记目录（小节标题 + 时间戳 + 字符数），长笔记先看目录再取节 | 读 |
| `get_note` | 读笔记正文；默认 `format="qa"`（省略截图/AI 总结），`section="对策"` 只取一节，`format="full"` 读整篇 | 读 |
| `get_transcript` | 读视频转写原话（支持时间窗切片与 `max_chars`） | 读 |
| `list_notes` | 浏览/发现笔记（默认 10 条、紧凑格式）；已知 task_id 时不要调用 | 读 |
| `index_status` | 后端健康 + 向量索引覆盖率 + 缺失清单 | 读 |
| `ask_notes` | 【兜底】BiliNote 内置在线 API 问答（RAG + 内置模型） | 在线 API |
| `create_note` | 提交视频 URL 生成笔记（异步，配 `get_note` 轮询） | 写 |
| `retry_task` | 重试失败任务 | 写 |
| `export_note` | 导出 PDF / Word 到指定目录 | 写 |
| `reindex_notes` | 补建缺失向量索引 | 写 |
| `delete_note` | 删除笔记（**不可恢复**，需 `confirm=true`） | 危险 |
| `cancel_task` | 停止生成中的任务（落 CANCELLED，可 retry） | 写 |

## 推荐用法（给 harness 的提示）

1. 回答与笔记相关的问题：先 `search_notes`；命中即可回答，不要为了确认再 `list_notes`。
2. 片段不够时，**换更具体的关键词再 search 一次**（“对策/案例/结论/原因”很有效）；
   仍不够再用 `note_outline` 看目录、`get_note(section="...")` 取指定小节。
   只有跨章节总结、导出、或用户明确要“整篇”时才 `get_note(format="full")`。
3. `ask_notes` 只在用户明确要求"用 BiliNote 自带问答"时使用。
4. 生成新笔记用 `create_note`，之后用 `get_note(task_id)` 轮询（通常几分钟）。
5. 检索结果异常时先 `index_status` 看覆盖率，缺的用 `reindex_notes` 补。

## 上下文与结果大小

- 所有工具都关闭了 MCP SDK 的 structured output 自动包装，避免同一段文本在
  `content` 和 `structuredContent` 里各回一遍（ZCode 等客户端会同时显示）。
- `search_notes` 默认 `top_k=5`、`snippet_chars=600`、`source="note"`；要原片用
  `source="transcript"`，要 AI 总结/目录用 `include_synthetic=true`。
- `get_note` 默认 `format="qa"`：去掉截图链接与 AI 总结；`section=` 只回一节；
  `max_chars=` 可按行边界截断并提示下一步。
- 需要量化每次结果大小时设 `BILINOTE_MCP_DEBUG=1`，server 会把
  `[bilinote-mcp] <tool>: N chars` 打到 stderr，不污染 stdio 协议。

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 「无法连接 BiliNote 后端」 | 桌面端没开；开了还报错就检查 `BILINOTE_BASE_URL` 端口 |
| 「接口不存在（404）：/api/chat/search」 | 后端版本过旧，重新部署含本功能的后端 |
| 「未配对（401）」 | 远程访问 token 错误；本机回环不应出现 |
| 「被拒绝（403）」 | Worker 关闭了远端配置，该操作需在本机执行 |
| 「尚未配置任何 LLM 供应商」 | 内置问答/生成笔记前需在设置页配好模型 |
| 检索结果偏少 | 相关性闸门宁缺勿噪；换关键词、调大 `top_k`（≤30） |
| 默认看不到截图 / AI 总结 | `get_note(format="full")`，或 `search_notes(include_synthetic=true)` |
| 检索看不到原片转写 | `search_notes(source="transcript")` 或 `source="all"` |
| 片段末尾有 `…` | 被 `snippet_chars` 截断；用 `get_note(section="小节标题")` 取该节全文 |

## 实现说明

- `backend/mcp_server.py`：stdio MCP server（`mcp<2` SDK + httpx 薄客户端），
  不 import 业务代码，启动即连。
- 后端新增 `POST /api/chat/search`：纯 chromadb 向量召回（本地嵌入），无 LLM 调用；
  已加入远程任务白名单。
- 环境变量：`BILINOTE_BASE_URL`（默认 `http://127.0.0.1:8483`）、
  `BILINOTE_TOKEN`（远程配对 token，自动转 `X-Pairing-Token` 头）。

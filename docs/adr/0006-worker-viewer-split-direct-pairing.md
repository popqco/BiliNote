# Worker 直连 Viewer 的一对一分体（无中继、无账号）

跨公网自用场景下，Viewer 直连 Worker 的 HTTP API（Viewer 填 Worker 地址加长效 token 配对，靠 Tailscale / ZeroTier 类组网打通），不设公网中继服务器、不做账号密码体系；Worker 是笔记唯一主存储且 LLM key、转写 key、cookie、代理等凭证只存 Worker，Viewer 按需拉取加本地缓存、设置页只是远管界面；首版只做一对一绑定，多 Worker 路由留待二期。

## Considered Options

- 公网中继服务器（两边都连服务器中转）：要长期运维云服务器，与自用场景不匹配。
- 账号密码注册登录体系：另一个量级的工作量，暂无需求。
- 两边各存全量笔记再双向同步：双主覆盖冲突难解；Worker 单主则无此问题。

## Consequences

- Worker 需同时 serve 前端与 API，并加 token 鉴权；本机保留完整 UI（即 All-in-One 多开一个对外入口）。
- 凭证类接口必须遮蔽：已有 key 只显示"已配置"加脱敏前后缀，修改须重输全量；Worker 本机留一个远控总开关，可一键禁止 Viewer 改全局配置。
- 通知复用 WxPusher / SMTP 加 Viewer 轮询，不做系统级推送。

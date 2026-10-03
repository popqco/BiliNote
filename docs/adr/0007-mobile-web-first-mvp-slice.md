# 移动端先响应式 Web 验证，MVP 只跑通跨公网链路

手机端第一步只做响应式 Web（Worker 顺手 serve 同一份前端，手机浏览器零部署直用），暂不套壳 App；手机 Viewer 信息架构目标是底部 Tab（新建 / 历史 / 设置），但 MVP 先接受桌面三栏布局在手机上"能看但挤"，跑通旧电脑 Worker 加手机浏览器 Viewer 的跨公网一条链路再重构体验；手机 Worker 是否重写、用什么语言，等旧电脑链路跑顺后按真实痛点二期立项（已排除无图形界面的硬塞方案，手机 Worker 若做则默认只走云端转写加云端 LLM，PC Worker 保留本地模型）。

## Considered Options

- 首版即套壳可安装 App（Tauri mobile / Capacitor）：签名、商店、sidecar 后端放哪等问题成本高，未经链路验证不值得。
- 全量一次做完（Tab 重构、远控开关、精细遮蔽全上）：链路未通前打磨体验，返工风险大。
- 无 GUI 的 Termux 式硬塞把现有 Python 后端塞进旧手机：配置痛苦，被明确否决。

## Consequences

- MVP = 旧电脑 Worker（serve 前端加 token 配对）加手机浏览器可提交可查看；远控总开关、key 脱敏精细化、移动端 Tab 均为链路通之后的事。
- 手机 Worker 二期立项时再定重写范围与语言，不在本次实现。

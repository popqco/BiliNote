# 桌面端交付：本地构建裸 exe 覆盖安装（暂不走仓库 CI）

仓库自带的 `.github/workflows/main.yml` 可以在 GitHub 云端构建完整安装包，但 fork 仓库的
Actions 默认处于「需手动启用」状态（本次未启用，也不希望用用户凭据去改仓库设置），因此本轮
采用**本地构建**：装 Rust（MSVC 工具链）+ Windows SDK 后，在 `BillNote_frontend` 执行
`pnpm/npm install` → `npx tauri build --no-bundle`，产物为
`src-tauri/target/release/BiliNote.exe`；把它**仅覆盖**安装目录里的 `app.exe`（先备份），
保留用户自有的 `BiliNoteBackend.exe`（venv 启动器）、`_internal`、`config/`、数据库与
WebView 数据（IndexedDB 历史记录不丢）。

选「只换 exe」而不是跑安装包：安装包会把官方 PyInstaller sidecar 与 `_internal` 一并装回，
覆盖掉本机「源码 + venv」的定制后端布局，等于回退到未修复的旧后端。构建注意点：
sidecar 占位文件需要 `bin/BiliNoteBackend/BiliNoteBackend-<target-triple>.exe` 形态，
`tauri build` 才会通过资源校验（`src-tauri/bin/` 已在 .gitignore 中）。

动机链：前端（夜间模式/徽章/自动化设置页等）只有重新构建才能进桌面应用 → 云端 CI 未启用
→ 本地工具链（rustup MSVC + Windows SDK 18362+）→ 裸 exe 覆盖。后续若在 fork 启用 GitHub
Actions，应切回云端构建以保证可复现（main.yml 已附带上传裸 exe 产物）。

# 装机版构建与部署流程（Windows 本机安装版）

> 适用范围：本 fork 的**本机安装版**部署——源码在本机构建，替换 `D:\Program Files\BiliNote`
> 下的运行时文件。上游的 Docker / 开发模式部署不在本文范围（见 README）。
>
> 本文是实际在用的流程沉淀，包含每个真实踩过的坑。改动越小，流程越短：只改前端时通常
> 1–2 分钟就能完成一轮「构建 → 替换 → 重启 → 验证」。

## 0. 组件拓扑（装了哪些东西在哪）

安装目录 `D:\Program Files\BiliNote`：

| 文件/目录 | 是什么 | 何时需要替换 |
| --- | --- | --- |
| `app.exe` | Tauri 桌面壳，**前端代码内嵌其中** | 每次前端改动 |
| `viewer-dist/` | 手机网页版（Viewer），由后端直服 | 每次前端改动（手机端同步更新） |
| `BiliNoteBackend.exe` + `BiliNoteBackend-x86_64-pc-windows-msvc.exe` | PyInstaller 打包的后端，**同一文件两个名字**（sidecar 约定名 + 主名） | 后端改动时 |
| `_internal/` | 后端的 Python 运行时与依赖（约 4GB，151 项） | 后端改动时 |
| `bili_note.db` / `config/` / `static/` / `logs/` | 数据库、运行配置、截图、日志 | **永不替换**（用户数据） |

源码仓库：`C:\Users\popqco\Documents\Codex\2026-09-24\new-chat\work\BiliNote-src-2`
（fork：[popqco/BiliNote](https://github.com/popqco/BiliNote)，`master` 分支）。

## 1. 构建前置

- 包管理器**必须用 pnpm**（npm 会装错依赖树）。
- Rust：`export PATH="$HOME/.cargo/bin:$PATH"`（Git Bash 默认没有 cargo）。
- 后端构建需要 venv（历史用 `bili-gpu-venv`）。

## 2. 构建

### 2.1 桌面端 app.exe（前端改动时）

```bash
cd BillNote_frontend
unset VITE_API_BASE_URL          # ⚠️ 必须！shell 里残留的 /api 会烘进桌面包导致图片全挂
export PATH="$HOME/.cargo/bin:$PATH"
pnpm tauri build
# 产物：src-tauri/target/release/app.exe
# 末尾 WiX light.exe 打包 msi 报错可忽略——部署用裸 exe，历来如此
```

### 2.2 手机网页版 viewer-dist（前端改动时）

```bash
cd BillNote_frontend
MSYS_NO_PATHCONV=1 VITE_API_BASE_URL=/api pnpm build
# ⚠️ MSYS_NO_PATHCONV=1 必写：否则 Git Bash 把 /api 转成磁盘路径，手机端白屏
# 产物：dist/
```

### 2.3 后端（后端改动时，通常可跳过）

```bash
cd backend
# venv 激活后：
pyinstaller BiliNoteBackend.spec --noconfirm
# 产物：dist/BiliNoteBackend/（BiliNoteBackend.exe + _internal/）
```

⚠️ `BiliNoteBackend.spec` 有过静默回退的教训：`pathex` 必须是 `'.'`，
`collect_all` 循环必须覆盖 chromadb / chromadb.rust_bindings / bcrypt / matplotlib /
docx / pymupdf / fitz / faster_whisper 这 8 个包。spec 回退会打出「能启动但功能全灭」的
白壳 exe（AI 问答全灭、包缺失等怪病都源于此）。改 spec 前先 diff 确认。

## 3. 部署（标准顺序）

```bash
# 1) 优雅关闭桌面端（会顺带带走它拉起的 sidecar 后端）
powershell -NoProfile -Command "
  \$app = Get-Process app -ErrorAction SilentlyContinue
  if (\$app) { \$app.CloseMainWindow() | Out-Null; Start-Sleep -Seconds 3 }
  \$app2 = Get-Process app -ErrorAction SilentlyContinue
  if (\$app2) { Stop-Process -Id \$app2.Id -Force; Start-Sleep -Seconds 2 }"

# 2) 备份（约定：原名 + .bak-YYYYMMDD-标签，回滚就靠它）
cd "/d/Program Files/BiliNote"
cp app.exe app.exe.bak-$(date +%Y%m%d)-<标签>

# 3) 替换
cp <源码>/src-tauri/target/release/app.exe app.exe                 # 桌面端
# 前端改动时同时更新 viewer：
MSYS_NO_PATHCONV=1 robocopy <源码>/BillNote_frontend/dist viewer-dist /MIR
# 后端改动时（额外）：exe 复制一份改名成 -x86_64-pc-windows-msvc.exe，
# _internal 用 robocopy /MIR 同步（先确认后端进程已死，运行中目录被锁 rename 必败）

# 4) 启动
powershell -NoProfile -Command "Start-Process 'D:\Program Files\BiliNote\app.exe' -WorkingDirectory 'D:\Program Files\BiliNote'"

# 5) 验证：轮询健康检查；首次冷启 16–40 秒属正常
for i in 1 2 3 4 5 6; do sleep 8
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:8483/api/sys_check")
  echo "try$i sys_check=$code"; [ "$code" = "200" ] && break
done
tail logs/app.log        # 确认 [startup 5/5] 启动完成、无 ERROR
```

回滚 = 关应用 → 用对应 `.bak-*` 文件覆盖回去 → 重启。

## 4. 已踩坑清单（按症状查）

| 症状 | 根因 | 对策 |
| --- | --- | --- |
| 桌面端图片/资源全挂 | shell 里 `VITE_API_BASE_URL=/api` 残留，烘进了桌面包 | 构建前 `unset VITE_API_BASE_URL` |
| 手机端白屏、配对转圈 | Git Bash 把 `/api` 转成磁盘路径 | viewer 构建必带 `MSYS_NO_PATHCONV=1` |
| 启动报「后端端口被占用」/ 405 误导 | 8483 被孤儿后端进程占着 | `Get-CimInstance Win32_Process` 找占用者点名（wmic 在本机返回空） |
| 假「后端已退出」红横幅 | 孤儿后端 vs 应用新拉的 sidecar 分裂 | 已修（启动探测收编健康后端）；若复发先查孤儿进程 |
| AI 问答全灭 / 某功能包缺失 | spec 回退（pathex / collect_all 丢失），白壳 exe | 按第 2.3 节核对 spec |
| 后端 exe 拉不起来（sidecar 报错） | sidecar 要求文件名三元组 | 后端 exe 必须同时有主名和 `-x86_64-pc-windows-msvc.exe` 副本 |
| `_internal` 替换失败 | 后端进程还在运行，目录被锁 | 先杀后端再 robocopy |
| 自动化轮全失败 | ffmpeg 未探测（`FFMPEG_BIN_PATH` 靠健康轮询前置 PATH） | 已修（轮前预检）；日志里 grep ffmpeg 确认 |
| `curl` exit 7 / sys_check=000 | 刚重启，后端首启慢 | 再等 25–40 秒重试 |

## 5. 手机端（Viewer）快速通道

手机与桌面同处一个 Tailscale 网络：手机浏览器开 `http://<桌面机组网IP>:8483/` →
设置 → 连接 Worker → 填 token（桌面端「设置 → 连接 Worker」页可查看本机 token）→ 配对。
完整步骤与验证清单见 [worker-viewer-deploy.md](./worker-viewer-deploy.md)。

## 6. 分发安装包（给另一台电脑一键安装）

`pnpm tauri build --bundles nsis` 产出 `src-tauri/target/release/bundle/nsis/BiliNote_<版本>_x64-setup.exe`，
内含桌面壳 + PyInstaller 后端（exe + `_internal`）+ 手机 viewer，新机器双击安装即开箱即用。

前置（顺序敏感）：

1. 后端产物就位 `src-tauri/bin/BiliNoteBackend/`：`BiliNoteBackend.exe`（+ 同内容三元组副本
   `BiliNoteBackend-x86_64-pc-windows-msvc.exe`，构建期 bundler 按三元组名取件）+ `_internal/`；
2. viewer 资源就位 `src-tauri/viewer-dist/`：用 `MSYS_NO_PATHCONV=1 VITE_API_BASE_URL=/api pnpm build`
   的产物拷入（`tauri.conf.json` resources 已声明，gitignored 不入库）；
3. ⚠️ **spec excludes torch**：torch 3.6GB 会把 NSIS 顶过 2GB 硬上限（makensis 32 位 mmap 越界，
   报 "error mmapping file ... out of range"）。已在 spec 排除——安装包版本地转写回退 CPU，
   Groq 云转写不受影响；本机部署不受影响（继续用旧 `_internal`，含 GPU）。

验证（2026-10-07 实测流程）：静默安装到临时目录
`setup.exe /S /D=C:\Temp\Test`（PowerShell `Start-Process -Wait` 最稳，Git Bash 直跑会被
参数转换坑）→ 文件树完整 → 免 `.env` 启动后端 → sys_check 200 + `GET /` 直服 viewer 200。
运行时 sidecar 解析为**无三元组后缀**的 `BiliNoteBackend.exe`（tauri-plugin-shell 2.3.5 源码确认），
与安装布局一致；三元组名只在构建期需要。

产出后手动上传：GitHub 仓库 → Releases → v2.5.0 → 编辑 → 拖入 setup.exe 发布。


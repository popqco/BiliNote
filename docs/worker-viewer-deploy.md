# Worker–Viewer 跨公网部署（Tailscale 直连版）

> 适用分支：`feat/mobile-split`（含票 1–5）。Viewer = 手机浏览器，Worker = 家里/旧电脑。
> 全程无中继、无账号：能连上组网 + 拿对 token 即视为本人。

## 1. Worker 侧（旧电脑 / 家里的电脑）一次性准备

1. 切到分支并装依赖：
   `git worktree add ../BiliNote-wt-mobile-split -b feat/mobile-split`（已建好可跳过），
   后端 `pip install -r requirements.txt`（或用已有的 `bili-gpu-venv`），前端 `pnpm install`。
2. 两台设备装 Tailscale 并登录同一账号，记下 Worker 的组网 IP（形如 `100.x.y.z`）。
   本次实测 Worker IP：`100.67.206.88`，手机：`100.109.166.11`。
3. 防火墙：Windows Defender 需放行 `TCP 8483` 入站（组网地址即可，不必放行公网）。
4. 构建前端 dist（**必须带 `MSYS_NO_PATHCONV=1`**，否则 Git Bash 会把 `/api` 转成 Windows 路径，
   烘出 `D:/Program Files/Git/api` 导致手机白屏配对转圈——本次真实踩坑）：
   `MSYS_NO_PATHCONV=1 VITE_API_BASE_URL=/api pnpm build`
5. 启动后端：`cd backend && python main.py`（默认 `0.0.0.0:8483`，直服 `dist`，同源零部署）。
   确认 `curl http://127.0.0.1:8483/api/sys_check` 返回 `{"code":0}`。

## 2. 手机配对（2 分钟）

1. Worker 本机浏览器打开 `http://127.0.0.1:8483/settings/connection`（本机回环免鉴），
   点「查看本机 token」→ 复制完整 token。
2. 手机浏览器（开 Tailscale）打开 `http://<Worker组网IP>:8483/`，
   点底部「设置」→ 第一项「连接 Worker」，填组网地址 + token →「配对并连接」。
3. 配对成功自动回首页，顶部琥珀色横幅消失。断开 = 同页「断开连接」，回到本机模式。

## 3. 日常使用与远控开关

- 新建 Tab 提交链接 → 笔记 Tab 看进度/正文 → 历史 Tab 回看。选中老任务切新建 Tab，
  表单回显该任务导出时的参数（老自动化笔记回退当前自动化配置）。
- 「连接 Worker」页底部有**远端配置总开关**：关闭后手机只能提交/查看任务，
  改全局配置 403 被拒；本机不受影响。**关闭只能在本机点**（防远端误锁），开启可在远端点。

## 4. 验证清单（照着点一遍）

- [ ] 手机根地址直开有内容（版本选择器 + 笔记正文 + 底部四 Tab）。
- [ ] 深链直开：`/settings` 菜单总览、`/settings/model`、`/settings/transcriber`、
      `/settings/download` 均有内容（曾因 `vite base:'./'` 整页白屏，已修）。
- [ ] 设置 9 项点进去、左上角返回都能回到菜单总览；模型列表↔表单、下载配置↔表单来回正常。
- [ ] 供应商编辑页 API Key 输入框为空、placeholder 显示「已配置（脱敏）」。
- [ ] 远控关闭后，手机改自动化配置被拒（toast 人话）、提交任务仍可用。
- [ ] 选中一篇自动化老笔记切新建 Tab：4 格式勾上、视频理解开（以当前自动化配置为准）。

## 5. 真实痛点记录（手机 Worker 二期立项输入）

1. **首屏 JS 体积敏感**：整站曾打成 8MB 单包（`Model` 包 3.4MB 是 `import * as Icons`  whole 图标库），
   Tailscale 下 `index` 包 2.2MB 拉 4 秒。已按路由拆包 + 图标改单文件 import（Model 包 → 16KB）。
   教训：任何新依赖先看分包体积，手机组网不是光纤。
2. **`MSYS_NO_PATHCONV=1` 必写进文档**：Git Bash 路径转换曾把 `VITE_API_BASE_URL=/api` 烘成磁盘路径，
   现象是手机配对转圈 60s 后报后端失败，极难联想到是构建期问题。
3. **深链直开是刚需**：手机浏览器习惯新标签开链接，`/settings/*` 深链必须首屏可用，
   SPA fallback + 相对 base 的组合是白屏重灾区（`base:'/'` + 绝对路径是正解）。
4. **vivo 自带浏览器只能看不能摸**：`uiautomator dump` 拿不到 WebView 内部节点，
   ADB tap 只能按坐标盲点。设置页回归只能逐页深链直开 + 截图，点菜单这类交互必须人手验。
5. **自动化失败有一类是配额/模型名问题**：实测 `space-bunny-free` 报过
   `invalid_model / 'model' is a required property`，手机端只看到红字，排查仍得回 Worker 看日志。
   后续可在 Viewer 加"失败原因 + 回 Worker 查日志"的引导。

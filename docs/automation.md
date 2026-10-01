# 自动化：稍后再看定期总结 + 通知

> 需求来源：定期检索 B 站「稍后再看」，为还没有笔记的视频自动生成总结，并把每轮结果汇总推送到微信/邮箱。
> 设计决策见 [ADR-0004](./adr/0004-dual-schedule-with-file-lock.md)。

## 前置条件（必读）

**必须配置 B 站 Cookie（SESSDATA）**——「稍后再看」是账号数据，未登录无法读取。

1. 浏览器登录 bilibili.com；
2. F12 → Application（应用）→ Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值
   （或直接复制整行 Cookie 字符串，格式 `SESSDATA=xxx; bili_jct=yyy; ...` 也支持）；
3. BiliNote →「设置 → 下载配置 → 哔哩哔哩 Cookie」粘贴保存。

Cookie 过期（一般数月）后：自动化会在日志里提示 Cookie 失效并停止，更新 Cookie 即恢复。

## 配置入口

「设置 → 自动化」：

| 配置项 | 说明 |
| --- | --- |
| 启用自动化 | 总开关（默认关） |
| 检查频率 | 默认每 120 分钟；应用启动后会补查一次（距上次检查超过半个周期时） |
| 总结范围 | 「全部未总结」= 稍后再看里所有没有成功笔记的视频；「仅最近新增」= 入单时间在最近 N 天内 |
| 每轮最多新提交 | 默认 5，防止一次把几百个视频灌进队列 |
| 生成配置 | 自动化专用：模型/供应商/风格/质量/视频理解开关，独立于首页表单 |
| 通知渠道 | WxPusher（微信）与 SMTP（邮箱），可各自独立启用 |

去重规则（一轮中每个视频只处理一次）：
- 已有**成功笔记**（数据库 video_tasks 命中）→ 跳过；
- 已有**未完成任务**（排队/生成中）→ 跳过；
- 超出时间窗 / 超出每轮上限 → 跳过（下一轮再处理）。

## 微信通知（WxPusher，免费）

1. 打开 https://wxpusher.zjiecode.com ，微信扫码登录；
2. 「应用管理 → 创建应用」→ 复制 **appToken**；
3. 微信关注「WxPusher」公众号（或扫描应用页二维码）→「我的 → 我的UID」→ 复制 **UID**；
4. 填入「设置 → 自动化 → 通知渠道」，勾选启用，点「发送测试通知」验证。

## 邮箱通知（SMTP）

以 QQ 邮箱为例：设置 → 账户 → 开启 SMTP 服务 → 生成**授权码**（不是登录密码）。

| 字段 | 值 |
| --- | --- |
| SMTP 服务器 | `smtp.qq.com`（163 为 `smtp.163.com`） |
| 端口 | `465`（SSL） |
| 账号 | 你的邮箱地址 |
| 授权码 | 上一步生成 |
| 收件人 | 接收通知的邮箱（可多个，逗号分隔） |

## 双轨调度：应用内 + Windows 计划任务

- **应用运行时**：后端内置调度线程按频率自动执行（推荐日常使用）。
- **应用关闭时**：注册 Windows 计划任务，由 `backend/automation_cli.py` 拉起单轮检查。
  两个入口通过 `config/automation.lock` 文件锁互斥；应用在运行时 CLI 会检测 8483 端口
  自动让位，不会重复执行。

注册（模板见 `scripts/register_watchlater_task.bat`，按注释改三个路径后运行）：

```bat
schtasks /Create /F /TN "BiliNote-WatchLater" ^
  /TR "D:\AIChat\run_watchlater_once.bat" /SC MINUTE /MO 120 /ST 00:05
```

运行器 `run_watchlater_once.bat` 内容（注意 CWD 必须是应用目录）：

```bat
@echo off
cd /d "D:\Program Files\BiliNote"
"<venv>\Scripts\python.exe" "<源码>\backend\automation_cli.py" >> "D:\Program Files\BiliNote\logs\automation_cli.log" 2>&1
```

移除：`schtasks /Delete /TN "BiliNote-WatchLater" /F`

## 手动触发与排查

- 「立即运行一轮」按钮：不等定时，立刻跑一轮（完成后发汇总通知）。
- CLI 日志：`D:\Program Files\BiliNote\logs\automation_cli.log`；
  应用内轮次日志：`D:\Program Files\BiliNote\logs\app.log`（搜「自动化检查轮」）。
- 常见问题：
  - 不执行 → 检查总开关；Cookie 是否有效；`config/automation.json` 是否存在。
  - 收不到通知 → 点「发送测试通知」看每渠道结果（渠道详情直接显示失败原因）。
  - 通知只发一条 → 设计如此：每轮结束发一条汇总（成功/失败清单）。

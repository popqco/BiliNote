@echo off
REM ============================================================
REM BiliNote「稍后再看」自动总结 — Windows 计划任务注册脚本（模板）
REM
REM 作用：应用没打开时，按计划拉起 backend/automation_cli.py 跑一轮检查。
REM      应用正在运行时会自动让位给应用内调度（CLI 检测 8483 端口后直接退出；
REM      两个入口另有 config/automation.lock 文件锁互斥，见 docs/adr/0004）。
REM
REM 使用前：把下面三个路径改成你的实际路径，然后运行本脚本。
REM 移除任务：schtasks /Delete /TN "BiliNote-WatchLater" /F
REM ============================================================

set "APP_DIR=D:\Program Files\BiliNote"
set "PYTHON=%USERPROFILE%\Documents\Codex\2026-09-24\new-chat\work\bili-gpu-venv\Scripts\python.exe"
set "CLI=%~dp0..\backend\automation_cli.py"
set "INTERVAL_MINUTES=120"

REM 生成实际执行用的小 bat（避免 schtasks 的嵌套引号地狱）
set "RUNNER=%APP_DIR%\run_watchlater_once.bat"
> "%RUNNER%" echo @echo off
>> "%RUNNER%" echo cd /d "%APP_DIR%"
>> "%RUNNER%" echo "%PYTHON%" "%CLI%" ^>^> "%APP_DIR%\logs\automation_cli.log" 2^>^&1

schtasks /Create /F /TN "BiliNote-WatchLater" /TR "%RUNNER%" /SC MINUTE /MO %INTERVAL_MINUTES% /ST 00:05

echo.
echo 已注册计划任务 BiliNote-WatchLater（每 %INTERVAL_MINUTES% 分钟，应用未运行时生效）
schtasks /Query /TN "BiliNote-WatchLater" /FO LIST

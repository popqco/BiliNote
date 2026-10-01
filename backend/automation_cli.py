"""Windows 计划任务入口：应用未运行时执行一轮「稍后再看」检查。

用法（工作目录必须是应用安装目录，保证 config/、note_results/、数据库路径
与桌面端完全一致）：

    cd /d "D:\\Program Files\\BiliNote"
    "<venv>\\Scripts\\python.exe" "<源码>\\backend\\automation_cli.py"

约定（见 docs/adr/0004）：
- 若桌面端后端正在监听 8483，说明应用已打开、由应用内调度负责，本进程直接退出；
- 与检查轮共用 config/automation.lock 文件锁，双入口互斥。
"""
import json
import os
import socket
import sys


def backend_running(port: int = 8483) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    if backend_running():
        print("[automation_cli] 桌面端后端正在运行，由应用内调度处理，退出。")
        return 0

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    try:
        from app.services.automation_config_manager import AutomationConfigManager
        from app.services.automation_scheduler import AutomationScheduler

        cfg = AutomationConfigManager().get_config()
        if not cfg.get("enabled"):
            print("[automation_cli] 自动化未启用（config/automation.json enabled=false），退出。")
            return 0

        result = AutomationScheduler().run_round_once_safe(cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"[automation_cli] 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

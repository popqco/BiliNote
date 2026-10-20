"""进程级数据根目录。

历史实现把 ``config/``、``note_results/``、``uploads/`` 写成 CWD 相对路径，
桌面端、Docker、CLI 与测试各自依赖启动目录。这里统一出口，默认值保持原样，
只保留同名环境变量覆盖，避免改变现有部署行为。
"""
import os
from pathlib import Path

CONFIG_DIR = Path(os.getenv("BILINOTE_CONFIG_DIR", "config"))
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
UPLOAD_DIR = Path(os.getenv("BILINOTE_UPLOAD_DIR", "uploads"))

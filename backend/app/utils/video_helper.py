import shutil
from pathlib import Path

from dotenv import load_dotenv
import subprocess

from app.utils.ffmpeg_limiter import ffmpeg_slots
from app.utils.proc import no_window_kwargs
import os
import uuid
load_dotenv()
api_path = os.getenv("API_BASE_URL", "http://localhost")
BACKEND_PORT= os.getenv("BACKEND_PORT", 8483)

BACKEND_BASE_URL = f"{api_path}:{BACKEND_PORT}"

from typing import Optional
# 原片截图的落盘宽度上限（2026-10-27）：源视频常见 2560/3840 宽，实测单张
# 4K JPEG 约 180KB、全库 3.5 万张占 6.7GB；阅读端最大只用到 1600px 缩略
#（放大看细节也用不到 4K）。压到 1920 宽 + q3，单张降到约 85KB，缩放后
# 仍比展示缩略更清晰。只缩不放：1280 宽的源视频保持原样。
SCREENSHOT_MAX_WIDTH = 1920
_SCALE_FILTER = f"scale=min({SCREENSHOT_MAX_WIDTH}\\,iw):-2"


def generate_screenshot(video_path: str, output_dir: str, timestamp: int, index: int) -> str:
    """
    使用 ffmpeg 生成截图，返回生成图片路径
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"screenshot_{index:03}_{uuid.uuid4()}.jpg"
    output_path = output_dir / filename

    command = [
        "ffmpeg",
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", _SCALE_FILTER,
        "-q:v", "3",
        str(output_path),
        "-y"
    ]

    print("Running command:", command)
    with ffmpeg_slots:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, **no_window_kwargs()
        )

    # 失败必须抛错而不是静默返回路径：调用方按「单张失败」降级处理，
    # 若仍返回路径，markdown 会写入指向不存在文件的 URL → 前端破图
    # （2026-10-05 截图链路稳定性排查确认的断点之一）
    if result.returncode != 0 or not output_path.exists():
        print("ffmpeg failed:", result.stderr[-500:] if result.stderr else "(no stderr)")
        raise RuntimeError(
            f"ffmpeg 截图失败 (t={timestamp}s, rc={result.returncode}): "
            f"{(result.stderr or '')[-200:]}"
        )

    return str(output_path)



def save_cover_to_static(local_cover_path: str, subfolder: Optional[str] = "cover") -> str:
    """
    将封面图片保存到 static 目录下，并返回前端可访问的路径
    :param local_cover_path: 本地原封面路径（比如提取出来的jpg）
    :param subfolder: 子目录，默认是 cover，可以自定义
    :return: 前端访问路径，例如 /static/cover/xxx.jpg
    """
    # 项目根目录
    project_root = os.getcwd()

    # static目录
    static_dir = os.path.join(project_root, "static")

    # 确定目标子目录
    target_dir = os.path.join(static_dir, subfolder or "cover")
    os.makedirs(target_dir, exist_ok=True)

    # 拷贝文件
    file_name = os.path.basename(local_cover_path)
    target_path = os.path.join(target_dir, file_name)
    shutil.copy2(local_cover_path, target_path)  # 保留原时间戳、权限
    image_relative_path = f"/static/{subfolder}/{file_name}".replace("\\", "/")
    url_path = f"{BACKEND_BASE_URL.rstrip('/')}/{image_relative_path.lstrip('/')}"
    # 返回前端可访问的路径
    return url_path

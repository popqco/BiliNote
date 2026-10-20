"""本地转写模型落盘检测。

原实现放在 ``app/routers/config.py``，导致 ``services/transcriber_config_manager``
反向 import 路由层；路由重组时符号消失，就绪检查被 ``except`` 吞掉并静默放行。
这里保持同一套目录判据，供路由与 service 共同使用。
"""
import os
from pathlib import Path

from app.utils.path_helper import get_model_dir


def check_whisper_model_exists(model_size: str, subdir: str = "whisper") -> bool:
    """检查指定 fast-whisper 模型是否已下载完整到本地。

    先把模型名 resolve 成可加载标识，再按类型判定：
      - 本地路径模型 → 直接看该目录下有没有 model.bin
      - HF repo_id → 看 HF cache 布局
        <model_dir>/models--{org}--{name}/snapshots/<hash>/model.bin
        （历史 modelscope 布局 <model_dir>/whisper-{size}/model.bin 也兼容识别）
    """
    from app.transcriber.whisper_models import (
        hf_cache_dirname,
        is_local_target,
        resolve_whisper_model,
    )

    try:
        target = resolve_whisper_model(model_size)
    except Exception:
        return False
    if is_local_target(target):
        return (Path(target) / "model.bin").exists()

    model_dir = Path(get_model_dir(subdir))
    hf_repo_dir = model_dir / hf_cache_dirname(target) / "snapshots"
    if hf_repo_dir.exists():
        for snapshot in hf_repo_dir.iterdir():
            if (snapshot / "model.bin").exists():
                return True
    legacy = model_dir / f"whisper-{model_size}" / "model.bin"
    return legacy.exists()


def check_mlx_whisper_model_exists(model_size: str) -> bool:
    """检查 mlx-whisper 模型是否已下载完整到本地。

    与 fast-whisper 的目录布局不同：mlx 模型按 HuggingFace repo_id
    （如 mlx-community/whisper-tiny-mlx）落盘，且没有 model.bin，
    用 config.json 作为「下载完成」的判据，和 mlx_whisper_transcriber.py 保持一致。
    """
    try:
        from app.transcriber.mlx_whisper_transcriber import MLX_MODEL_MAP
    except Exception:
        return False
    repo_id = MLX_MODEL_MAP.get(model_size)
    if not repo_id:
        return False
    model_dir = get_model_dir("mlx-whisper")
    model_path = os.path.join(model_dir, repo_id)
    return (Path(model_path) / "config.json").exists()

"""JSON 配置文件读写。

六个配置 manager 原先各自实现一份 ``_read/_write``，写盘也不是原子的。
这里只收敛机制，不改变各 manager 的取值语义：

- ``read()``：文件缺失/损坏/顶层不是对象 → ``{}``，由调用方用默认值补齐；
- ``write()``：同目录临时文件 + ``os.replace``，避免半截 JSON；
- ``read_merged()/update()``：以默认值为底的深合并，供 automation 这类嵌套配置使用；
- ``resolve_int()``：文件 > 环境变量 > 默认 + 夹取，供 task 这类数值配置使用。
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def deep_merge(base: Optional[dict], patch: Optional[dict]) -> Dict[str, Any]:
    """递归合并 ``patch`` 到 ``base``；只覆盖出现的键，不丢嵌套默认值。"""
    out: Dict[str, Any] = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_int(
    file_value: Any,
    env_name: str,
    default: int,
    lo: int,
    hi: int,
) -> int:
    """数值配置的生效值：文件值 > 环境变量 > 默认，非法值回落到默认，最后夹取。"""
    raw = file_value if file_value is not None else os.getenv(env_name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


class JsonFileStore:
    """一个 JSON 文件的读写根；不缓存，调用方每次拿到最新内容。"""

    def __init__(self, path: str | Path, defaults: Optional[dict] = None):
        self.path = Path(path)
        self.defaults = defaults if defaults is not None else {}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write(self, data: dict) -> None:
        """原子写：同目录临时文件 fsync 后 replace。"""
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def ensure_exists(self) -> None:
        """首次使用时落一份默认文件；已有文件不覆盖。"""
        if not self.path.exists():
            self.write(dict(self.defaults))

    def read_merged(self) -> Dict[str, Any]:
        return deep_merge(self.defaults, self.read())

    def update(self, patch: Optional[dict] = None) -> Dict[str, Any]:
        merged = deep_merge(self.read_merged(), patch or {})
        self.write(merged)
        return merged

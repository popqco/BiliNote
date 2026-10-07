"""Worker 顺手 serve 前端 dist（票 2）：手机浏览器打开 Worker 根地址即完整 Viewer。

约定：
- dist 目录：环境变量 FRONTEND_DIST，缺省为 <repo>/BillNote_frontend/dist；
  找不到 index.html 时返回 False、调用方跳过，不影响 /api。
- /api、/static、/uploads 与已注册路由优先（本函数最后挂载，只接未命中的 GET）。
- SPA fallback：未知路径回 index.html；但 /api/* 未命中必须是 404 JSON，不能回页面。
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from app.utils.logger import get_logger

logger = get_logger(__name__)


def resolve_dist_dir(explicit: str | None = None) -> Path | None:
    raw = explicit or os.getenv("FRONTEND_DIST", "")
    if raw.strip():
        candidate = Path(raw.strip()).expanduser()
    else:
        # 装机版（PyInstaller onedir）：_internal/app/frontend_dist.py 的
        # parents[2] == 安装根目录，Tauri 安装包把 viewer-dist 资源放在根目录
        # ——新机器没有 .env/FRONTEND_DIST，靠这个默认值直服手机 Viewer。
        frozen_root = Path(__file__).resolve().parents[2]
        candidate = frozen_root / "viewer-dist"
        if not (candidate / "index.html").is_file():
            # 源码开发模式：backend/app/frontend_dist.py -> parents[2] == 仓库根
            candidate = frozen_root / "BillNote_frontend" / "dist"
    if (candidate / "index.html").is_file():
        return candidate
    return None


def mount_frontend_dist(app: FastAPI, dist_dir: Path | str | None = None) -> bool:
    dist = Path(dist_dir) if dist_dir is not None else resolve_dist_dir()
    if dist is None or not (dist / "index.html").is_file():
        logger.info("前端 dist 不存在，跳过 Viewer 直服（仅提供 /api）")
        return False

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_viewer(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"code": 404, "msg": "接口不存在", "data": None},
            )
        candidate = dist / full_path
        # 只服务 dist 内的真实文件，防止 ../ 越界（resolve 后必须仍在 dist 内）
        try:
            resolved = candidate.resolve()
            if full_path and resolved.is_file() and resolved.is_relative_to(dist.resolve()):
                # index.html 永不缓存：Viewer 是手机浏览器直连的"远程后台"，
                # 后端升级后若手机还用旧 index 就会去拉已不存在的旧分包
                # （404 HTML 当 JS 解析 → 白屏/功能停留在旧版）。
                # JS/CSS 等带 hash 分包照常走 StaticFiles 强缓存，不受影响。
                if resolved.name == "index.html":
                    return FileResponse(
                        resolved,
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
                return FileResponse(resolved)
        except Exception:
            pass
        return FileResponse(
            dist / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    logger.info(f"已挂载前端 dist 直服 Viewer：{dist}")
    return True

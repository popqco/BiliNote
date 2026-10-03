from fastapi import FastAPI

from .middleware.pairing_auth import PairingAuthMiddleware
from .routers import note, provider, model, config, chat, export



# 注意：export 路由必须保留——mobile-split 分支曾删过它（PDF/Word 导出全灭），
# 合并 viewer 功能时只加配对鉴权中间件，不动任何已有路由。
def create_app(lifespan, enable_pairing_auth: bool = True) -> FastAPI:
    app = FastAPI(title="BiliNote",lifespan=lifespan)
    app.include_router(note.router, prefix="/api")
    app.include_router(provider.router, prefix="/api")
    app.include_router(model.router,prefix="/api")
    app.include_router(config.router,  prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(export.router, prefix="/api")

    if enable_pairing_auth:
        app.add_middleware(PairingAuthMiddleware)

    return app

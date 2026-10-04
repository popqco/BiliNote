from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.services.pairing_manager import PairingManager

# 配对前必须可达：连通性探测 + 配对校验 + Viewer 配对页自身的静态资源。
# 注意 /api/sys_health 仍需鉴权（它会回显转写器配置等部署细节）。
_PUBLIC_PATHS = (
    "/api/sys_check",
    "/api/pairing_status",
    "/api/pairing_verify",
    "/api/port_check",
)

# 远控关闭时远端 Viewer 仍可用的"任务白名单"（票 5）：
# 提交任务、查状态/历史、取元信息、看封面、AI 问答——只读任务数据 + 提交新任务，
# 不涉及任何全局配置改动。注意这里是前缀匹配（带 task_id 的子路径同样放行）。
# /api/model_list（启用模型下拉）与 /api/pairing_*（本机面板经配对 token 调用）
# 同样放行：前者是提交表单的必要数据，后者走 token 调用且本机回环本来就免鉴。
_REMOTE_TASK_PREFIXES = (
    "/api/generate_note",
    "/api/task_status/",
    "/api/tasks/recent",
    "/api/video_meta",
    "/api/image_proxy",
    "/api/chat/ask",
    "/api/chat/status",
    "/api/model_list",
    "/api/model_enable",
    "/api/pairing_token",
    "/api/pairing_regenerate",
    "/api/remote_config",
)
_REMOTE_TASK_EXACT = frozenset({
    "/api/model_list",
})


def _client_is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    return client.host in ("127.0.0.1", "::1")


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    # 非 /api 下的都是 Viewer 静态资源（票 2 由后端 serve 的前端 dist）
    if not path.startswith("/api"):
        return True
    return False


def _is_remote_task_path(path: str) -> bool:
    """远控关闭时仍允许远端 Viewer 访问的任务类路径（提交/查询/历史/封面/问答）。"""
    if path in _REMOTE_TASK_EXACT:
        return True
    return any(path == p or path.startswith(p) for p in _REMOTE_TASK_PREFIXES)


class PairingAuthMiddleware(BaseHTTPMiddleware):
    """Worker 配对鉴权：远端 /api 请求需带正确 token，本机回环免鉴。

    约定：Viewer 在 Authorization 头或 X-Pairing-Token 头中携带 token。
    远控总开关（票 5）：allow_remote=False 时，已配对的远端 Viewer 也只能走
    任务白名单（提交/查询/历史/封面/问答），改配置类接口一律 403；
    本机回环不受开关影响（All-in-One 照常用）。
    """

    def __init__(self, app, manager: PairingManager | None = None):
        super().__init__(app)
        self.manager = manager or PairingManager()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        # 浏览器 CORS 预检不带自定义头，必须直接放行，否则手机浏览器连 OPTIONS 都发不出去
        if request.method == "OPTIONS":
            return await call_next(request)
        if _is_public_path(path) or _client_is_loopback(request):
            return await call_next(request)
        # <img> 发不出自定义头：封面 image_proxy 允许 ?pairing_token= 传 token（仅图片场景使用，header 优先）。
        token = (
            request.headers.get("x-pairing-token")
            or _bearer_token(request)
            or request.query_params.get("pairing_token")
            or request.query_params.get("token")
        )
        if not self.manager.verify(token):
            return JSONResponse(
                status_code=401,
                content={"code": 401, "msg": "未配对：请在 Viewer 填写 Worker 地址与配对 token", "data": None},
            )
        # 已配对但远控关闭：非任务白名单一律 403（本机回环前面已放行，不受影响）。
        if not self.manager.get_allow_remote() and not _is_remote_task_path(path):
            return JSONResponse(
                status_code=403,
                content={"code": 403, "msg": "Worker 已关闭远端配置：仅可提交与查看任务，改配置请到 Worker 本机操作", "data": None},
            )
        return await call_next(request)


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None

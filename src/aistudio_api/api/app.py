"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_logging_settings,
)
from aistudio_api.infrastructure.utils.logger import (
    dump_request_exchange,
    get_logger,
    is_dump_requests_enabled,
    setup_logging,
)

from .dependencies import require_api_key, require_web_auth
from .routes_accounts import router as accounts_router
from .routes_gemini import router as gemini_router
from .routes_models import router as models_router
from .routes_openai import router as openai_router
from .routes_system import (
    protected_router as system_protected_router,
    public_router as system_public_router,
)
from .state import runtime_state

_log_cfg = get_configured_logging_settings()
setup_logging(str(_log_cfg.get("level", "INFO")))
logger = get_logger("server")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.account.account_store import AccountStore
    from aistudio_api.infrastructure.browser.browser_engine import (
        install_process_cleanup_handlers,
    )

    install_process_cleanup_handlers()

    # 上游通道选择（2026-10-20 用户指派引入）：aistudio（默认，浏览器+账号池）/
    # vertex_express（官方 Key 直连）/ console_graphql（Cloud 控制台 Cookie 直连）。
    # 后两条无浏览器、无账号池语义，错误原样透传、零自动重试。
    # 三通道并存（2026-10-20）：pg/ 无头浏览器（默认）+ express/ + cookie/，
    # 按模型名前缀选路（infrastructure/upstreams/registry.py）。AISTUDIO_UPSTREAM
    # 仅作为「默认通道」的覆盖项；各通道只在凭据齐备时注册。
    from aistudio_api.infrastructure.upstreams.registry import (
        UpstreamRegistry,
    )

    registry = UpstreamRegistry()
    # 单浏览器（用户 2026-10-20 拍板：不再要浏览器 worker 池）；账号池保留——
    # 多账号在这台浏览器上轮换（蓝绿热切，在途流不中断）。
    client = AIStudioClient(port=runtime_state.browser_port)
    runtime_state.client = client
    registry.register("aistudio", client)

    default_override = str(getattr(settings, "upstream_mode", "") or "").lower()
    if default_override in ("vertex_express", "console_graphql"):
        # 无前缀模型也指到 override 通道（仍可用 pg/ 强制走浏览器）
        registry.default_channel = default_override

    if settings.vertex_express_api_key:
        from aistudio_api.infrastructure.upstreams.vertex_express import (
            VertexExpressClient,
        )

        registry.register(
            "vertex_express",
            VertexExpressClient(
                settings.vertex_express_api_key,
                project_id=settings.google_project_id,
                location=settings.express_location,
                proxy_url=settings.proxy_url,
            ),
        )
        logger.info("upstream registered: express/")
    if settings.console_cookie and settings.console_project_id:
        from aistudio_api.infrastructure.upstreams.console_graphql import (
            ConsoleGraphqlClient,
        )

        try:
            registry.register(
                "console_graphql",
                ConsoleGraphqlClient(
                    settings.console_cookie,
                    settings.console_project_id,
                    proxy_url=settings.proxy_url,
                ),
            )
            logger.info("upstream registered: cookie/")
        except ValueError as exc:
            logger.warning("cookie/ 通道未注册: %s", exc)
    runtime_state.upstreams = registry

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)
    runtime_state.account_service = account_service

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)
    runtime_state.rotator = rotator

    logger.info(
        "Client initialized (port=%s, accounts=%d)",
        runtime_state.browser_port,
        len(account_store.list_accounts()),
    )

    # 后台预热浏览器，避免首次请求延迟
    warmup_task = None

    async def _warmup():
        for attempt in range(3):
            try:
                await client.warmup()
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("浏览器预热失败: %s", e)

    if account_store.list_accounts():
        warmup_task = asyncio.create_task(_warmup())

    idle_task = None
    if settings.browser_idle_timeout > 0:

        async def _idle_monitor():
            while True:
                await asyncio.sleep(10.0)
                try:
                    active_client = runtime_state.client
                    if active_client is None:
                        continue
                    session = active_client._session
                    if session is not None:
                        await session.check_idle_timeout()
                except Exception as e:
                    logger.debug("空闲回收检查异常: %s", e)

        idle_task = asyncio.create_task(_idle_monitor())

    tier_task = None

    async def _auto_refresh_tiers():
        import json

        from aistudio_api.infrastructure.account.cookie_parser import (
            probe_account_benefit_tier,
            probe_account_email,
        )

        for a in account_store.list_accounts():
            if not a.email or a.tier == "Free":
                apath = account_store.get_auth_path(a.id)
                if apath and apath.is_file():
                    try:
                        adata = json.loads(apath.read_text(encoding="utf-8"))
                        rcookies = adata.get("cookies") or []
                        cdict = {
                            str(c.get("name") or ""): str(c.get("value") or "")
                            for c in rcookies
                            if isinstance(c, dict) and c.get("name")
                        }
                        if cdict:
                            if not a.email:
                                em = await probe_account_email(
                                    cdict, auth_user=a.auth_user or "0"
                                )
                                if em:
                                    account_store.update_account_email(a.id, em)
                            if a.tier == "Free":
                                t = await probe_account_benefit_tier(
                                    cdict, auth_user=a.auth_user or "0"
                                )
                                if t and t != "Free":
                                    account_store.update_account_tier(a.id, t)
                                    logger.info(
                                        "启动自动校验并升级账号 %s (u/%s) 订阅等级: %s",
                                        a.id,
                                        a.auth_user,
                                        t,
                                    )
                    except Exception:
                        pass
    tier_task = asyncio.create_task(_auto_refresh_tiers())

    yield
    logger.info("服务正在关闭")
    if warmup_task and not warmup_task.done():
        warmup_task.cancel()
    if idle_task and not idle_task.done():
        idle_task.cancel()
    if tier_task and not tier_task.done():
        tier_task.cancel()
    runtime_state.save_stats()
    if runtime_state.rotator is not None:
        runtime_state.rotator.save_state()
    express_client = registry.get("vertex_express")
    if express_client is not None:
        await express_client.close()
    from aistudio_api.application.sdk_bridge import close_sdk_clients

    await close_sdk_clients()
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端失败: %s", e)
    runtime_state.client = None
    runtime_state.upstreams = None
    runtime_state.account_service = None
    runtime_state.rotator = None


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_public_router)
app.include_router(system_protected_router, dependencies=[Depends(require_web_auth)])
app.include_router(accounts_router, prefix="/api", dependencies=[Depends(require_web_auth)])
app.include_router(accounts_router, dependencies=[Depends(require_web_auth)])
app.include_router(gemini_router, dependencies=[Depends(require_api_key)])
app.include_router(models_router, dependencies=[Depends(require_api_key)])
# OpenAI 兼容端口（/v1/chat/completions、/v1/models）：与 Gemini 端口共用同一套上游链路
app.include_router(openai_router, dependencies=[Depends(require_api_key)])


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    if _request.url.path.startswith("/v1/") and not _request.url.path.startswith(
        "/v1beta"
    ):
        # OpenAI 客户端：错误体按 OpenAI 形状给（依赖注入阶段的 401/503 也走这条）
        from aistudio_api.application.openai_bridge import openai_error_payload

        return JSONResponse(
            status_code=exc.status_code,
            content=openai_error_payload(exc),
            headers=getattr(exc, "headers", None),
        )
    code, msg, status_str = classify_gemini_error_payload(exc)
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    if _request.url.path.startswith("/v1/") and not _request.url.path.startswith(
        "/v1beta"
    ):
        # OpenAI 客户端按 error.message / error.code 解析错误
        from aistudio_api.application.openai_bridge import openai_error_payload

        return JSONResponse(status_code=400, content=openai_error_payload(exc))
    code, msg, status_str = 400, str(exc), "INVALID_ARGUMENT"
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
    )


@app.middleware("http")
async def logging_and_dump_middleware(request, call_next):
    """统一请求生命周期日志与请求转储 (DEBUG) 中间件。"""

    start_time = time.perf_counter()
    path = request.url.path

    is_static = path.startswith(("/static", "/assets")) or path in ("/favicon.ico",)
    dump_enabled = is_dump_requests_enabled() and not is_static

    req_id = ""
    body_text = None
    client_str = ""
    headers_dict = None
    query_dict = None

    if dump_enabled:
        req_id = f"req_{secrets.token_hex(4)}"
        client_str = (
            f"{request.client.host}:{request.client.port}" if request.client else "unknown"
        )
        headers_dict = dict(request.headers)
        query_dict = dict(request.query_params)
        try:
            body_bytes = await request.body()
            body_text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            body_text = "(failed to read request body)"

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if dump_enabled:
            dump_request_exchange(
                req_id=req_id,
                method=request.method,
                url=str(request.url),
                client=client_str,
                headers=headers_dict or {},
                query_params=query_dict or {},
                body_text=body_text,
                status_code=500,
                elapsed_ms=elapsed_ms,
                response_text=f"Internal Server Error (Exception: {exc})",
            )
        else:
            logger.error(
                "HTTP %s %s -> 500 (%.1fms, %s)",
                request.method,
                path,
                elapsed_ms,
                exc,
            )
        raise exc

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    content_type = response.headers.get("content-type", "")
    is_event_stream = "text/event-stream" in content_type

    if dump_enabled:
        resp_text = None
        if is_event_stream:
            resp_text = "(streaming response in progress)"
        else:
            try:
                # 读取非流式响应体并重构 iterator 以便正常返回给客户端
                chunks = [chunk async for chunk in response.body_iterator]
                resp_bytes = b"".join(chunks)
                resp_text = resp_bytes.decode("utf-8", errors="replace")

                async def _replay_iterator():
                    for c in chunks:
                        yield c

                response.body_iterator = _replay_iterator()
            except Exception:
                resp_text = "(failed to read response body)"

        dump_request_exchange(
            req_id=req_id,
            method=request.method,
            url=str(request.url),
            client=client_str,
            headers=headers_dict or {},
            query_params=query_dict or {},
            body_text=body_text,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response_headers=dict(response.headers),
            response_text=resp_text,
            is_stream=is_event_stream,
        )
    elif not is_static and response.status_code >= 400:
        # 2xx 的非 LLM 请求（健康检查、面板轮询、静态资源）不写日志：
        # 它们在终端里只是噪声，真正的 LLM 请求日志由各路由自己带 request id 打。
        logger.warning(
            "HTTP %s %s -> %d (%.1fms)",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )

    return response


# 挂载前端静态资源与 SPA 路由支持
static_dir = Path(__file__).resolve().parents[1] / "static"
index_html_path = static_dir / "index.html"

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/auth/check")
async def auth_check():
    """检查认证状态，用于前端判断是否需要登录。"""
    from aistudio_api.config import settings

    return {"auth_enabled": settings.auth_enabled}


@app.middleware("http")
async def spa_navigation_middleware(request: Request, call_next):
    """统一处理前端 SPA 单页应用导航：浏览器页面直达/刷新响应 index.html，API 请求直通 JSON。"""
    accept = request.headers.get("accept", "")
    is_html_nav = (
        request.method == "GET"
        and "text/html" in accept
        and "application/json" not in accept
    )
    is_api = request.url.path.startswith(
        (
            "/api",
            "/v1",  # 含 /v1beta（Gemini）与 /v1/chat/completions、/v1/models（OpenAI）
            "/auth",
            "/assets",
            "/static",
            "/health",
            "/stats",
            "/rotation",
            "/config",
            "/api-keys",
        )
    )
    is_accounts_spa = request.url.path in ("/accounts", "/accounts/")
    if is_html_nav and (is_accounts_spa or not is_api) and index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    response = await call_next(request)

    if request.url.path.startswith(("/accounts", "/api/accounts")) or is_html_nav:
        existing_vary = response.headers.get("Vary")
        if existing_vary:
            if "Accept" not in [v.strip() for v in existing_vary.split(",")]:
                response.headers["Vary"] = f"{existing_vary}, Accept"
        else:
            response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

    return response


@app.get("/")
async def root_index():
    """根路径兜底响应。"""
    if index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response
    return JSONResponse({"message": "AI Studio API Web UI"})


def main():
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--host",
        default=settings.host,
        help="监听地址；默认 0.0.0.0（局域网可访问），只本机用 127.0.0.1",
    )
    parser.add_argument("--browser-port", type=int, default=settings.browser_port)
    args = parser.parse_args()

    runtime_state.browser_port = args.browser_port
    settings.port = args.port
    settings.host = args.host
    settings.browser_port = args.browser_port

    import uvicorn

    logger.info("正在启动服务，监听 %s:%s", args.host, args.port)
    # access_log=False：uvicorn 的逐请求访问日志与 LLM 请求日志重复且全是噪声
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)

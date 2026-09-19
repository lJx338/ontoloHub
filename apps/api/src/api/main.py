"""FastAPI 应用入口"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.cache import (
    get_cache,
    ping_cache_loop,
    reset_cache,
)
from src.core.config import settings
from src.db.connection import init_db, close_db
from src.api.auth import ensure_bootstrap_admin


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理"""
    # 启动
    await init_db()
    # 确保 bootstrap 管理员存在（HIA-51）。失败不阻断启动，但记录原因。
    try:
        await ensure_bootstrap_admin()
    except Exception as exc:  # pragma: no cover — 不让启动被 DB 抖动阻断
        import logging
        logging.getLogger(__name__).warning(
            "bootstrap admin failed: %s", exc
        )
    # 启动 Redis 缓存（HIA-64）。连不上不阻断启动，只记日志。
    try:
        cache = await get_cache()
        healthy = await cache.ping()
        if not healthy:
            import logging
            logging.getLogger(__name__).warning(
                "Redis cache unavailable at startup; running in degraded mode"
            )
    except Exception as exc:  # pragma: no cover
        import logging
        logging.getLogger(__name__).warning(
            "cache init failed (continuing without cache): %s", exc
        )

    # 后台循环：每 30s 探一次 Redis 健康
    stop_event = asyncio.Event()
    ping_task = asyncio.create_task(ping_cache_loop(stop_event, interval=30.0))
    app.state.cache_ping_stop = stop_event
    app.state.cache_ping_task = ping_task

    # HIA-75 C3: 启动 cron 触发器调度器（每 60s 检查一次）
    scheduler_stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(_cron_loop(scheduler_stop_event, interval=60.0))
    app.state.scheduler_stop = scheduler_stop_event
    app.state.scheduler_task = scheduler_task

    yield

    # 关闭
    stop_event.set()
    try:
        await asyncio.wait_for(ping_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    scheduler_stop_event.set()
    try:
        await asyncio.wait_for(scheduler_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    await reset_cache()
    await close_db()


async def _cron_loop(stop_event: asyncio.Event, interval: float = 60.0) -> None:
    """HIA-75 C3: Cron 触发器循环，每 60s 扫一次 schedule 触发器。

    简单实现：每次循环都遍历所有 ACTIVE schedule 触发器，匹配当前时间。
    对于 1 分钟精度的 cron 表达式足够；高精度需求可后续接入 APScheduler。
    """
    import logging
    logger = logging.getLogger(__name__)
    from src.services.webhook_dispatcher import run_cron_triggers

    while not stop_event.is_set():
        try:
            await run_cron_triggers()
        except Exception as exc:
            logger.warning("cron loop iteration failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


# 创建 FastAPI 应用
app = FastAPI(
    title=settings.api.title,
    version=settings.api.version,
    description="本体工程与语义应用工作台 API",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# 配置 CORS（HIA-51：允许前端/客户端通过自定义 header 传当前用户身份）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.security.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-User-Id",
        "X-User-Email",
        "X-User-Name",
        "X-Request-Id",
    ],
    expose_headers=["X-Request-Id"],
)


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局异常处理"""
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": str(exc),
            "path": str(request.url),
        },
    )


# 健康检查
@app.get("/health")
async def health_check() -> dict:
    """健康检查 — 报告 DB / Redis 健康状态（HIA-64）。"""
    from src.core.cache import get_cache, CacheUnavailable

    redis_ok: bool = False
    redis_error: str | None = None
    try:
        cache = await get_cache()
        redis_ok = await cache.ping()
    except CacheUnavailable as e:
        redis_error = str(e)
    except Exception as e:  # pragma: no cover - 兜底
        redis_error = f"{type(e).__name__}: {e}"

    return {
        "status": "healthy",
        "version": settings.api.version,
        "redis": {
            "enabled": settings.redis.enabled,
            "healthy": redis_ok,
            "error": redis_error,
        },
    }


# 根路径
@app.get("/")
async def root() -> dict:
    """根路径"""
    return {
        "name": "OntoloHub",
        "version": settings.api.version,
        "description": "本体工程与语义应用工作台",
        "docs": "/api/docs",
    }


# 业务路由
from src.api import projects, ontologies, mappings, sources, proposals, catalog
from src.api import candidates, validation
from src.api import auth_api, users, members, audit

app.include_router(projects.router)
app.include_router(ontologies.router)
app.include_router(catalog.router)
app.include_router(mappings.router)
app.include_router(sources.router)
app.include_router(proposals.router)
app.include_router(candidates.router)
app.include_router(validation.router)

# HIA-51: 身份 / 成员 / 审计
app.include_router(auth_api.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(members.router)
app.include_router(audit.router)

# HIA-64 B1: JWT + API Key 认证（HIA-51 之上叠加）
from src.api import auth_jwt
app.include_router(auth_jwt.auth_router, prefix="/api")
app.include_router(auth_jwt.apikey_router, prefix="/api")

# Evidence 收件箱 / 剖析执行
from src.api.sources import ev_router, prof_router
app.include_router(ev_router)
app.include_router(prof_router)

# HIA-59: 对象与链接运行时 API
from src.api import objects
app.include_router(objects.router)

# HIA-71: Connector 框架 API
from src.api import connectors
app.include_router(connectors.router)

# 发布与交付路由
from src.api.release import router as release_router, cr_router as change_request_router
app.include_router(release_router)
app.include_router(change_request_router)

# HIA-70 C1: Action / Function 编辑器 API（ActionType CRUD + ActionRun 执行）
from src.api.action import router as action_router
app.include_router(action_router)

# HIA-75 C3: Webhook / Trigger 集成 API
from src.api.webhooks import router as webhooks_router, trigger_router
app.include_router(webhooks_router)
app.include_router(trigger_router)

# HIA-76 C4: Workflow 编排 API
from src.api.workflow import router as workflow_router, exec_router as workflow_exec_router
app.include_router(workflow_router)
app.include_router(workflow_exec_router)

# HIA-77 D1: Workspace / multi-tenant API
from src.api.workspace import router as workspace_router, user_router as workspace_user_router
app.include_router(workspace_router)
app.include_router(workspace_user_router)

# HIA-74 D4: 版本化发布线（分支 / tag / merge audit）
from src.api.release_line import router as release_line_router, merge_router as release_line_merge_router
app.include_router(release_line_router)
app.include_router(release_line_merge_router)


@app.get("/api")
async def api_root() -> dict:
    """API 根路径"""
    return {
        "version": settings.api.version,
        "endpoints": {
            "projects": "/projects",
            "ontologies": "/ontologies",
            "catalog": "/catalog",
            "mappings": "/mappings",
            "sources": "/sources",
            "proposals": "/proposals",
            "candidates": "/candidates",
            "validation": "/validation",
            "evidence_upload": "/projects/{id}/evidences/upload",
            "evidence_inbox": "/evidences/project/{id}",
            "profiling": "/profiling",
            "objects": "/objects/projects/{id}/objects",
            "links": "/objects/projects/{id}/links",
            "connectors": "/connectors",
            "connector_types": "/connectors/types",
            "releases": "/releases",
            "change_requests": "/change-requests",
            "deployments": "/deployments",
            "actions": "/projects/{id}/actions",
            "action_runs": "/projects/{id}/action-runs",
            "workflows": "/projects/{id}/workflows",
            "workflow_executions": "/projects/{id}/workflow-executions",
            "workspaces": "/api/workspaces",
            "workspace_members": "/api/workspaces/{id}/members",
            "user_workspaces": "/api/users/me/workspaces",
            "workspace_quota": "/api/workspaces/{id}/quota",
            "users": "/api/users",
            "members": "/projects/{id}/members",
            "audit": "/projects/{id}/audit",
            "audit_verify": "/audit/verify",
            "auth_login": "/api/auth/login",
            "auth_refresh": "/api/auth/refresh",
            "auth_me": "/api/auth/me",
            "auth_set_password": "/api/auth/set-password",
            "auth_bootstrap": "/api/auth/bootstrap",
            "api_keys": "/api/api-keys",
        },
    }

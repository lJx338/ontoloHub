"""FastAPI 应用入口"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    yield
    # 关闭
    await close_db()


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
    """健康检查"""
    return {
        "status": "healthy",
        "version": settings.api.version,
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

# Evidence 收件箱 / 剖析执行
from src.api.sources import ev_router, prof_router
app.include_router(ev_router)
app.include_router(prof_router)

# 发布与交付路由
from src.api.release import router as release_router, cr_router as change_request_router
app.include_router(release_router)
app.include_router(change_request_router)


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
            "evidences": "/evidences",
            "profiling": "/profiling",
            "releases": "/releases",
            "change_requests": "/change-requests",
            "deployments": "/deployments",
            "users": "/api/users",
            "members": "/projects/{id}/members",
            "audit": "/projects/{id}/audit",
            "audit_verify": "/audit/verify",
        },
    }

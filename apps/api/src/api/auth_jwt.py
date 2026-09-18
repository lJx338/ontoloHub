"""JWT + API Key 认证 API（HIA-64 B1 — 多用户系统）。

端点：
- ``POST /api/auth/login``              — 邮箱 + 密码 → JWT (access + refresh)
- ``POST /api/auth/refresh``            — refresh token → 新 access token
- ``GET  /api/auth/me``                 — 当前用户信息
- ``POST /api/auth/set-password``       — 给当前用户（或管理员代别人）设密码

API Key：
- ``GET    /api/api-keys``              — 列出当前用户的 key
- ``POST   /api/api-keys``              — 创建 key（明文仅返回一次）
- ``DELETE /api/api-keys/{id}``         — 撤销 key（软删：revoked_at）
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    CurrentPrincipal,
    get_current_user,
    record_audit,
    require_role,
)
from src.core.auth import (
    authenticate_user,
    constant_time_eq,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    get_user_by_id,
    hash_api_key,
    hash_password,
)
from src.db.connection import get_session
from src.db.governance import AuditEventType
from src.db.identity import ApiKey, User

logger = logging.getLogger(__name__)


# ===========================================================================
# Auth router: login / refresh / me / set-password
# ===========================================================================

auth_router = APIRouter(prefix="/auth", tags=["auth"])


# ---------- request / response models ----------


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token TTL（秒）


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str
    global_role: str
    is_active: bool
    last_login_at: Optional[str]

    model_config = {"from_attributes": True}


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=255)
    # 仅管理员可代别人改：必须传目标 user_id
    target_user_id: Optional[str] = None
    # 改自己密码时必须传旧密码（防 token 泄漏后被滥用）
    current_password: Optional[str] = None


# ---------- endpoints ----------


@auth_router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """邮箱 + 密码 → JWT access + refresh token。"""
    user = await authenticate_user(session, email=body.email.strip().lower(), password=body.password)
    if user is None:
        # 不区分 email 不存在 vs 密码错 → 一律 401（防 enumeration）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)
    await session.flush()

    from src.core.config import get_settings
    settings = get_settings()

    access = create_access_token(subject=str(user.id), extra={"email": user.email})
    refresh = create_refresh_token(subject=str(user.id))
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,  # 用 CREATE 表达"产生 session"
        principal=CurrentPrincipal(user=user, is_admin=user.global_role == "admin"),
        target_type="auth_login",
        target_id=str(user.id),
        target_label=user.email,
        request=request,
        after={"method": "password"},
    )

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        token_type="bearer",
        expires_in=settings.security.jwt_access_ttl_seconds,
    )


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """用 refresh token 换新的 access token。"""
    try:
        payload = decode_token(body.refresh_token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid refresh token: {e}",
        ) from e

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not a refresh token",
        )

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid refresh token payload",
        )

    try:
        user_uuid = uuid.UUID(user_id_str)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid sub: {e}",
        ) from e

    user = await get_user_by_id(session, user_uuid)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
        )

    from src.core.config import get_settings
    settings = get_settings()

    access = create_access_token(subject=str(user.id), extra={"email": user.email})
    new_refresh = create_refresh_token(subject=str(user.id))
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        token_type="bearer",
        expires_in=settings.security.jwt_access_ttl_seconds,
    )


@auth_router.get("/me", response_model=MeResponse)
async def me(
    principal: CurrentPrincipal = Depends(get_current_user),
) -> MeResponse:
    """返回当前已认证用户的信息。"""
    user = principal.user
    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        global_role=user.global_role,
        is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@auth_router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
async def set_password(
    body: SetPasswordRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """设置 / 重置密码。

    - 改自己：必须传 ``current_password``（普通用户）
    - 改别人：必须传 ``target_user_id`` 且调用方是 admin
    """
    if body.target_user_id:
        # 代别人改 — 只允许 admin
        if not principal.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin required to set other user's password",
            )
        try:
            target_uuid = uuid.UUID(body.target_user_id)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid target_user_id: {e}",
            ) from e
        target = await get_user_by_id(session, target_uuid)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="target user not found",
            )
    else:
        # 改自己 — 必须传 current_password
        target = principal.user
        if not body.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_password required when changing own password",
            )
        from src.core.auth import verify_password
        if not verify_password(body.current_password, target.password_hash or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="current password is wrong",
            )

    target.password_hash = hash_password(body.new_password)
    session.add(target)
    await session.flush()
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        target_type="user_password",
        target_id=str(target.id),
        target_label=target.email,
        request=request,
        after={"by": "self" if not body.target_user_id else "admin"},
    )


# ===========================================================================
# API Key router
# ===========================================================================

apikey_router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


class ApiKeyCreatedResponse(BaseModel):
    """创建 key 的响应 — 明文 plain_key 仅返回这一次！"""

    id: str
    name: str
    plain_key: str  # 仅创建时返回；后续 GET 看不到
    key_prefix: str
    scopes: list[str]
    expires_at: Optional[str]
    created_at: str


class ApiKeyListItem(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    last_used_at: Optional[str]
    expires_at: Optional[str]
    revoked_at: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


@apikey_router.get("", response_model=list[ApiKeyListItem])
async def list_api_keys(
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyListItem]:
    """列出当前用户的所有 API Key（不含明文）。"""
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_id == principal.user.id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    return [
        ApiKeyListItem(
            id=str(k.id),
            name=k.name,
            key_prefix=k.key_prefix,
            scopes=k.scopes or [],
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            expires_at=k.expires_at.isoformat() if k.expires_at else None,
            revoked_at=k.revoked_at.isoformat() if k.revoked_at else None,
            created_at=k.created_at.isoformat(),
        )
        for k in keys
    ]


@apikey_router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    request: Request,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreatedResponse:
    """创建 API Key。明文 plain_key 只在这次响应里返回。"""
    plain, key_prefix, key_hash = generate_api_key()

    expires_at = None
    if body.expires_in_days:
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    api_key = ApiKey(
        user_id=principal.user.id,
        project_id=None,  # 全局 key；项目级 key 后续可加
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=body.scopes,
        expires_at=expires_at,
    )
    session.add(api_key)
    await session.flush()
    await session.refresh(api_key)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        target_type="api_key",
        target_id=str(api_key.id),
        target_label=api_key.name,
        request=request,
        after={"name": api_key.name, "scopes": api_key.scopes, "key_prefix": api_key.key_prefix},
    )

    return ApiKeyCreatedResponse(
        id=str(api_key.id),
        name=api_key.name,
        plain_key=plain,  # ⚠️ 仅此一次
        key_prefix=api_key.key_prefix,
        scopes=api_key.scopes or [],
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        created_at=api_key.created_at.isoformat(),
    )


@apikey_router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """撤销 API Key（软删 — 设 revoked_at）。"""
    result = await session.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == principal.user.id,  # 只能撤销自己的
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="api key not found",
        )
    if api_key.revoked_at is not None:
        # 幂等：已撤销过的不报错
        return
    api_key.revoked_at = datetime.now(timezone.utc)
    session.add(api_key)
    await session.flush()
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        target_type="api_key",
        target_id=str(api_key.id),
        target_label=api_key.name,
        request=request,
    )

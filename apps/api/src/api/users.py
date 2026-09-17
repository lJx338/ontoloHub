"""用户管理 API（HIA-51 M1-01）。

M0/M1 阶段没有正式 IAM；本接口提供：
- ``GET /api/users``            列出所有用户（任何已登录用户）
- ``POST /api/users``          新建用户（要求全局 ADMIN 或 bootstrap）
- ``GET /api/users/me``        返回当前用户及其全部成员关系
- ``GET /api/users/{id}``      按 id 获取（任何已登录）
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import User, Membership, GlobalRole, Role
from src.api.auth import (
    CurrentPrincipal,
    get_current_user,
)


router = APIRouter(prefix="/users", tags=["identity"])


# --------- Schemas ---------

class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(..., min_length=1, max_length=255)
    global_role: GlobalRole = GlobalRole.USER


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    global_role: str
    is_active: bool
    last_login_at: Optional[str] = None
    created_at: str

    model_config = {"from_attributes": True}


class MembershipResponse(BaseModel):
    project_id: uuid.UUID
    role: str
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class MeResponse(BaseModel):
    user: UserResponse
    memberships: list[MembershipResponse]
    is_admin: bool


# --------- Routes ---------

@router.get("", response_model=list[UserResponse])
async def list_users(
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[UserResponse]:
    result = await session.execute(
        select(User).order_by(User.created_at.asc())
    )
    users = result.scalars().all()
    return [_to_response(u) for u in users]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: UserCreate,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Create a new user (requires global ADMIN)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    existing = await session.execute(
        select(User).where(User.email == data.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email already in use")
    user = User(
        email=data.email,
        display_name=data.display_name,
        global_role=data.global_role.value,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return _to_response(user)


@router.get("/me", response_model=MeResponse)
async def get_me(
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    result = await session.execute(
        select(Membership).where(
            Membership.user_id == principal.user.id,
            Membership.is_active.is_(True),
        )
    )
    memberships = result.scalars().all()
    return MeResponse(
        user=_to_response(principal.user),
        memberships=[
            MembershipResponse(
                project_id=m.project_id,
                role=m.role,
                is_active=m.is_active,
                created_at=m.created_at.isoformat(),
            )
            for m in memberships
        ],
        is_admin=principal.is_admin,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _to_response(user)


# --------- Helpers ---------

def _to_response(u: User) -> UserResponse:
    return UserResponse(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        global_role=u.global_role,
        is_active=u.is_active,
        last_login_at=u.last_login_at.isoformat() if u.last_login_at else None,
        created_at=u.created_at.isoformat(),
    )

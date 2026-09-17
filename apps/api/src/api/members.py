"""项目成员管理 API（HIA-51 M1-01）。

路径: ``/projects/{project_id}/members`` — 仅 OWNER 可写，其他人可读成员列表。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import User, Membership, Role, GlobalRole
from src.api.auth import (
    CurrentPrincipal,
    coerce_diff,
    record_audit,
    require_role,
)
from src.db.governance import AuditEventType

router = APIRouter(prefix="/projects/{project_id}/members", tags=["members"])


# --------- Schemas ---------

class MemberInvite(BaseModel):
    """Add a user to a project, creating the user if it doesn't exist yet."""
    email: EmailStr
    display_name: Optional[str] = None
    role: Role = Role.VIEWER


class MemberUpdate(BaseModel):
    role: Role


class MemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_active: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# --------- Routes ---------

@router.get("", response_model=list[MemberResponse])
async def list_members(
    project_id: uuid.UUID = Path(..., description="project scope"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[MemberResponse]:
    """List all members of the project (any role can read)."""
    principal, _ = ctx
    result = await session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.project_id == project_id)
        .order_by(Membership.created_at.asc())
    )
    out: list[MemberResponse] = []
    for m, u in result.all():
        out.append(_to_member_response(m, u.email, u.display_name))
    return out


@router.post("", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
async def invite_member(
    data: MemberInvite,
    project_id: uuid.UUID = Path(..., description="project scope"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    principal, _ = ctx
    normalized = data.email.strip().lower()
    user = await _ensure_user(session, normalized, data.display_name)
    existing = await session.execute(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.project_id == project_id,
        )
    )
    m = existing.scalar_one_or_none()
    if m is not None:
        before = coerce_diff(m)
        m.role = data.role.value
        m.is_active = True
        m.invited_by = principal.user.id
        await session.flush()
        # flush 后 m 的非主键列可能被 expire；先 refresh 再读。
        await session.refresh(m)
        await record_audit(
            session,
            event_type=AuditEventType.UPDATE,
            principal=principal,
            project_id=project_id,
            target_type="membership",
            target_id=str(m.id),
            target_label=f"{user.email}@{data.role.value}",
            before=before,
            after=coerce_diff(m),
        )
        return _to_member_response(m, user.email, user.display_name)

    m = Membership(
        user_id=user.id,
        project_id=project_id,
        role=data.role.value,
        invited_by=principal.user.id,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    # flush 后 m 的非主键列可能被 expire；先 refresh 再读。
    await session.refresh(m)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="membership",
        target_id=str(m.id),
        target_label=f"{user.email}@{data.role.value}",
        after=coerce_diff(m),
    )
    return _to_member_response(m, user.email, user.display_name)


@router.patch("/{user_id}", response_model=MemberResponse)
async def update_member_role(
    user_id: uuid.UUID,
    data: MemberUpdate,
    project_id: uuid.UUID = Path(..., description="project scope"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    principal, _ = ctx
    target = await session.execute(
        select(Membership).where(
            Membership.user_id == user_id,
            Membership.project_id == project_id,
        )
    )
    m = target.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="membership not found")
    user = await session.execute(select(User).where(User.id == user_id))
    u = user.scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")

    before = coerce_diff(m)
    m.role = data.role.value
    await session.flush()
    # flush 后 m/u 的非主键列可能被 expire；显式 refresh 后再访问。
    await session.refresh(m)
    await session.refresh(u)
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        target_type="membership",
        target_id=str(m.id),
        target_label=f"{u.email}@{data.role.value}",
        before=before,
        after=coerce_diff(m),
    )
    return _to_member_response(m, u.email, u.display_name)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    project_id: uuid.UUID = Path(..., description="project scope"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> None:
    principal, _ = ctx
    target = await session.execute(
        select(Membership).where(
            Membership.user_id == user_id,
            Membership.project_id == project_id,
        )
    )
    m = target.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="membership not found")
    before = coerce_diff(m)
    m.is_active = False
    await session.flush()
    # flush 后 m 的非主键列可能被 expire（onupdate 触发的 server default fetch
    # 也会引发一次 async SELECT）；先 refresh 再读，避免在 coerce_diff 同步路径
    # 中触发 MissingGreenlet。
    await session.refresh(m)
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        project_id=project_id,
        target_type="membership",
        target_id=str(m.id),
        before=before,
        after=coerce_diff(m),
    )


# --------- Helpers ---------

async def _ensure_user(
    session: AsyncSession, email: str, display_name: Optional[str]
) -> User:
    result = await session.execute(select(User).where(User.email == email))
    u = result.scalar_one_or_none()
    if u is not None:
        return u
    u = User(
        email=email,
        display_name=display_name or email.split("@")[0],
        global_role=GlobalRole.USER.value,
        is_active=True,
    )
    session.add(u)
    await session.flush()
    await session.refresh(u)
    return u


def _to_member_response(m: Membership, email: str, display_name: str) -> MemberResponse:
    return MemberResponse(
        id=m.id,
        user_id=m.user_id,
        email=email,
        display_name=display_name,
        role=Role(m.role),
        is_active=m.is_active,
        created_at=m.created_at.isoformat() if m.created_at else "",
        updated_at=m.updated_at.isoformat() if m.updated_at else "",
    )

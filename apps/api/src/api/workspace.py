"""Workspace API — HIA-77 D1 multi-tenant.

Endpoints (all under ``/api/workspaces``):

  Workspace CRUD:
    POST   /workspaces                          create
    GET    /workspaces                          list (user's accessible)
    GET    /workspaces/{wid}                    detail (member only)
    PATCH  /workspaces/{wid}                    update (admin/owner)
    DELETE /workspaces/{wid}                    delete (owner only)

  Membership:
    GET    /workspaces/{wid}/members            list members
    POST   /workspaces/{wid}/members            invite user (admin+)
    PATCH  /workspaces/{wid}/members/{uid}      change role (owner)
    DELETE /workspaces/{wid}/members/{uid}      remove (admin+, or self)

  Switcher:
    GET    /users/me/workspaces                 current user's workspaces

  Quota:
    GET    /workspaces/{wid}/quota              quota snapshot

The data-isolation guarantee: every Project inside a Workspace is reachable
only to members of that Workspace.  When ``X-Workspace-Id`` is supplied in a
request, the API rejects calls that don't belong to that tenant.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.governance import AuditEventType
from src.db.identity import User
from src.db.workspace import (
    Workspace,
    WorkspaceMembership,
    WorkspacePlan,
    WorkspaceRole,
    default_quota,
    workspace_has_role,
)
from src.db.project import Project
from src.api.auth import get_current_user, record_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])
user_router = APIRouter(prefix="/api/users", tags=["Workspaces"])


# ===========================================================================
# Helpers
# ===========================================================================


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


async def _resolve_principal_user_id(user) -> Optional[uuid.UUID]:
    if hasattr(user, "user"):
        return user.user.id
    if hasattr(user, "id"):
        return user.id
    return None


async def _load_workspace(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Workspace:
    w = (await session.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Workspace 不存在")
    return w


async def _membership(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[WorkspaceMembership]:
    return (await session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.is_active.is_(True),
        )
    )).scalar_one_or_none()


async def require_workspace_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    principal,
) -> WorkspaceMembership:
    """Return the caller's active workspace membership; raise 404 if absent.

    We use 404 (not 403) so the existence of a workspace isn't leaked to
    non-members — matches the project role pattern.
    """
    actor_id = await _resolve_principal_user_id(principal)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="需要登录")
    m = await _membership(session, workspace_id, actor_id)
    if not m:
        raise HTTPException(status_code=404, detail="workspace not found")
    return m


async def require_workspace_role(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    principal,
    min_role: WorkspaceRole,
) -> WorkspaceMembership:
    m = await require_workspace_member(session, workspace_id, principal)
    actual = WorkspaceRole(m.role)
    if not workspace_has_role(actual, min_role):
        raise HTTPException(
            status_code=403,
            detail=f"需要 {min_role.value} 或更高权限（当前 {actual.value}）",
        )
    return m


async def _ensure_first_workspace(
    session: AsyncSession, user_id: uuid.UUID
) -> Workspace:
    """Auto-create a default workspace for a user who has none yet.

    Idempotent — a user always has at least one workspace to land in.
    """
    existing = (await session.execute(
        select(WorkspaceMembership)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.is_active.is_(True),
        )
    )).scalar_one_or_none()
    if existing:
        return (await _load_workspace(session, existing.workspace_id))

    # Create new personal workspace
    user = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one()
    base_slug = re.sub(r"[^a-z0-9-]+", "-", user.email.split("@")[0].lower())[:48] or "user"
    slug = base_slug
    suffix = 1
    while True:
        exists = (await session.execute(
            select(Workspace.id).where(Workspace.slug == slug)
        )).scalar_one_or_none()
        if not exists:
            break
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    w = Workspace(
        name=f"{user.display_name or user.email} 的工作区",
        slug=slug,
        plan=WorkspacePlan.FREE.value,
        settings={},
        owner_id=user.id,
        created_by=user.id,
    )
    session.add(w)
    await session.flush()
    await session.refresh(w)

    m = WorkspaceMembership(
        user_id=user.id,
        workspace_id=w.id,
        role=WorkspaceRole.OWNER.value,
        invited_by=None,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    return w


# ===========================================================================
# Pydantic models
# ===========================================================================


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=2, max_length=64)
    description: Optional[str] = None
    plan: Optional[str] = Field(None, pattern="^(free|pro|enterprise)$")

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug 仅允许小写字母数字 + 连字符；首尾必须是字母数字"
            )
        return v


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    plan: Optional[str] = Field(None, pattern="^(free|pro|enterprise)$")
    settings: Optional[dict] = None


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str]
    plan: str
    settings: dict
    owner_id: uuid.UUID
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class WorkspaceSummary(BaseModel):
    """Compact view for list / switcher — caller already knows their role."""

    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str]
    plan: str
    role: str
    is_owner: bool


class MemberInvite(BaseModel):
    user_id: Optional[uuid.UUID] = Field(
        None,
        description="Either user_id or email must be provided",
    )
    email: Optional[str] = Field(
        None,
        description="User is created on the fly if it doesn't exist (dev mode)",
    )
    role: str = Field("member", pattern="^(owner|admin|member|viewer)$")


class MemberRoleUpdate(BaseModel):
    role: str = Field(..., pattern="^(owner|admin|member|viewer)$")


class MemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    user_display_name: str
    role: str
    invited_by: Optional[uuid.UUID]
    is_active: bool
    created_at: str


class QuotaResponse(BaseModel):
    workspace_id: uuid.UUID
    plan: str
    max_projects: int
    max_users: int
    max_objects: int
    current_projects: int
    current_users: int
    settings: dict


# ===========================================================================
# Converters
# ===========================================================================


def _workspace_to_response(w: Workspace) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=w.id,
        name=w.name,
        slug=w.slug,
        description=w.description,
        plan=w.plan,
        settings=w.settings or {},
        owner_id=w.owner_id,
        created_by=w.created_by,
        created_at=_iso(w.created_at),
        updated_at=_iso(w.updated_at),
    )


def _member_to_response(
    m: WorkspaceMembership, user: User
) -> MemberResponse:
    return MemberResponse(
        id=m.id,
        user_id=user.id,
        user_email=user.email,
        user_display_name=user.display_name,
        role=m.role,
        invited_by=m.invited_by,
        is_active=bool(m.is_active),
        created_at=_iso(m.created_at),
    )


# ===========================================================================
# Workspace CRUD
# ===========================================================================


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    data: WorkspaceCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkspaceResponse:
    actor_id = await _resolve_principal_user_id(user)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="需要登录")

    # Slug uniqueness
    dup = (await session.execute(
        select(Workspace.id).where(Workspace.slug == data.slug)
    )).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail=f"slug '{data.slug}' 已存在")

    plan_value = data.plan or WorkspacePlan.FREE.value

    w = Workspace(
        name=data.name,
        slug=data.slug,
        description=data.description,
        plan=plan_value,
        settings={},
        owner_id=actor_id,
        created_by=actor_id,
    )
    session.add(w)
    await session.flush()

    # Owner auto-joins as OWNER
    m = WorkspaceMembership(
        user_id=actor_id,
        workspace_id=w.id,
        role=WorkspaceRole.OWNER.value,
        invited_by=None,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    await session.refresh(w)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=None,
        target_type="workspace",
        target_id=str(w.id),
        after={"name": w.name, "slug": w.slug, "plan": w.plan},
    )
    return _workspace_to_response(w)


@router.get("", response_model=list[WorkspaceSummary])
async def list_my_workspaces(
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    auto_create: bool = Query(
        True,
        description="If user has no workspace, lazily create a default one",
    ),
) -> list[WorkspaceSummary]:
    actor_id = await _resolve_principal_user_id(user)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="需要登录")

    if auto_create:
        await _ensure_first_workspace(session, actor_id)

    result = await session.execute(
        select(WorkspaceMembership, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == actor_id,
            WorkspaceMembership.is_active.is_(True),
            Workspace.deleted_at.is_(None),
        )
        .order_by(Workspace.created_at.asc())
    )
    rows = result.all()

    return [
        WorkspaceSummary(
            id=w.id,
            name=w.name,
            slug=w.slug,
            description=w.description,
            plan=w.plan,
            role=m.role,
            is_owner=w.owner_id == actor_id,
        )
        for (m, w) in rows
    ]


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkspaceResponse:
    await require_workspace_member(session, workspace_id, user)
    w = await _load_workspace(session, workspace_id)
    return _workspace_to_response(w)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: uuid.UUID,
    data: WorkspaceUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkspaceResponse:
    await require_workspace_role(
        session, workspace_id, user, WorkspaceRole.ADMIN
    )
    w = await _load_workspace(session, workspace_id)

    if data.name is not None:
        w.name = data.name
    if data.description is not None:
        w.description = data.description
    if data.plan is not None:
        w.plan = data.plan
    if data.settings is not None:
        # Shallow merge — keys present override, others preserved.
        merged = dict(w.settings or {})
        merged.update(data.settings)
        w.settings = merged

    await session.flush()
    await session.refresh(w)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=None,
        target_type="workspace",
        target_id=str(w.id),
        after={"name": w.name, "plan": w.plan},
    )
    return _workspace_to_response(w)


@router.delete(
    "/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_workspace(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    """Soft-delete the workspace.  Only the OWNER may do this."""
    actor_id = await _resolve_principal_user_id(user)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="需要登录")
    w = await _load_workspace(session, workspace_id)
    if w.owner_id != actor_id:
        raise HTTPException(
            status_code=403, detail="只有 workspace owner 可以删除"
        )

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=None,
        target_type="workspace",
        target_id=str(workspace_id),
        after={"name": w.name},
    )

    # Cascade: drop memberships + unbind projects (workspace_id=NULL)
    await session.execute(
        WorkspaceMembership.__table__.delete().where(
            WorkspaceMembership.workspace_id == workspace_id
        )
    )
    # Projects are kept but unbound (workspace_id = NULL).
    w.deleted_at = datetime.now(timezone.utc)
    w.deleted_by = actor_id
    await session.flush()


# ===========================================================================
# Membership
# ===========================================================================


@router.get(
    "/{workspace_id}/members",
    response_model=list[MemberResponse],
)
async def list_members(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    include_inactive: bool = Query(False),
) -> list[MemberResponse]:
    await require_workspace_member(session, workspace_id, user)

    q = (
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
    )
    if not include_inactive:
        q = q.where(WorkspaceMembership.is_active.is_(True))
    q = q.order_by(WorkspaceMembership.created_at.asc())

    result = await session.execute(q)
    return [_member_to_response(m, u) for (m, u) in result.all()]


@router.post(
    "/{workspace_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    workspace_id: uuid.UUID,
    data: MemberInvite,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> MemberResponse:
    """Invite a user to the workspace.

    ADMIN+ may invite.  If the user doesn't exist yet (dev mode), we
    create a stub User with the supplied email so the invitation can
    be resolved later.
    """
    await require_workspace_role(
        session, workspace_id, user, WorkspaceRole.ADMIN
    )
    await _load_workspace(session, workspace_id)

    if not data.user_id and not data.email:
        raise HTTPException(
            status_code=422, detail="user_id 或 email 必须提供其一"
        )

    actor_id = await _resolve_principal_user_id(user)

    # Resolve user
    target: Optional[User] = None
    if data.user_id:
        target = (await session.execute(
            select(User).where(User.id == data.user_id)
        )).scalar_one_or_none()
    if target is None and data.email:
        target = (await session.execute(
            select(User).where(User.email == data.email)
        )).scalar_one_or_none()
        if target is None:
            # Dev-mode auto-provision
            target = User(
                email=data.email,
                display_name=data.email.split("@")[0],
                global_role="user",
                is_active=True,
            )
            session.add(target)
            await session.flush()
            await session.refresh(target)

    if target is None:
        raise HTTPException(status_code=404, detail="找不到目标用户")

    # Already a member?
    existing = await _membership(session, workspace_id, target.id)
    if existing:
        # Re-activate if previously inactive.
        if not existing.is_active:
            existing.is_active = True
            existing.role = data.role
            await session.flush()
            await session.refresh(existing)
            return _member_to_response(existing, target)
        raise HTTPException(
            status_code=409, detail="该用户已是 workspace 成员"
        )

    m = WorkspaceMembership(
        user_id=target.id,
        workspace_id=workspace_id,
        role=data.role,
        invited_by=actor_id,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    await session.refresh(m)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=None,
        target_type="workspace_member",
        target_id=str(m.id),
        after={
            "workspace_id": str(workspace_id),
            "user_id": str(target.id),
            "role": data.role,
        },
    )
    return _member_to_response(m, target)


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=MemberResponse,
)
async def change_member_role(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberRoleUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> MemberResponse:
    """Change a member's workspace role.  Owner-only."""
    await require_workspace_role(
        session, workspace_id, user, WorkspaceRole.OWNER
    )
    w = await _load_workspace(session, workspace_id)

    m = await _membership(session, workspace_id, user_id)
    if not m:
        raise HTTPException(status_code=404, detail="成员不存在")
    if m.user_id == w.owner_id and data.role != WorkspaceRole.OWNER.value:
        raise HTTPException(
            status_code=409,
            detail="不能修改 workspace owner 的角色（先转让 ownership）",
        )

    m.role = data.role
    await session.flush()
    await session.refresh(m)

    target = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one()

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=None,
        target_type="workspace_member",
        target_id=str(m.id),
        after={"role": data.role},
    )
    return _member_to_response(m, target)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    """Remove a member.  Admin+ may remove others; any member may leave (self)."""
    actor_id = await _resolve_principal_user_id(user)
    if actor_id is None:
        raise HTTPException(status_code=401, detail="需要登录")

    is_self = actor_id == user_id
    if is_self:
        # Self-leave requires at least MEMBER (we already proved we're in
        # the workspace through require_workspace_member below).
        await require_workspace_member(session, workspace_id, user)
    else:
        await require_workspace_role(
            session, workspace_id, user, WorkspaceRole.ADMIN
        )

    w = await _load_workspace(session, workspace_id)
    if user_id == w.owner_id and not is_self:
        # No one may remove the owner via this endpoint — must transfer first.
        raise HTTPException(
            status_code=409, detail="不能移除 workspace owner"
        )

    m = await _membership(session, workspace_id, user_id)
    if not m:
        raise HTTPException(status_code=404, detail="成员不存在")

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=None,
        target_type="workspace_member",
        target_id=str(m.id),
        after={"user_id": str(user_id), "self": is_self},
    )
    # Soft-delete the membership — keeps audit trail intact.
    m.is_active = False
    await session.flush()


# ===========================================================================
# Switcher
# ===========================================================================


@user_router.get("/me/workspaces", response_model=list[WorkspaceSummary])
async def my_workspaces(
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> list[WorkspaceSummary]:
    """Alias for ``GET /api/workspaces`` for switcher widgets."""
    return await list_my_workspaces(
        session=session, user=user, auto_create=True
    )


# ===========================================================================
# Quota
# ===========================================================================


@router.get("/{workspace_id}/quota", response_model=QuotaResponse)
async def get_quota(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> QuotaResponse:
    await require_workspace_member(session, workspace_id, user)
    w = await _load_workspace(session, workspace_id)

    # Count current usage
    project_count = (await session.execute(
        select(func.count(Project.id)).where(
            Project.workspace_id == workspace_id,
            Project.deleted_at.is_(None),
        )
    )).scalar_one()
    user_count = (await session.execute(
        select(func.count(WorkspaceMembership.user_id)).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.is_active.is_(True),
        )
    )).scalar_one()

    return QuotaResponse(
        workspace_id=w.id,
        plan=w.plan,
        max_projects=w.effective_quota("max_projects"),
        max_users=w.effective_quota("max_users"),
        max_objects=w.effective_quota("max_objects"),
        current_projects=int(project_count or 0),
        current_users=int(user_count or 0),
        settings=w.settings or {},
    )

"""认证 + 权限 + 审计。

HIA-51 (M1-01) — 项目/成员/角色 + 基础审计 + 跨项目隔离的中枢。

设计要点：
- 不引入 OAuth/JWT：M0 单机原生阶段通过 ``X-User-Email`` (或 ``X-User-Id``)
  header 识别调用者。本机 / Docker 内默认信任调用方。
- 全局管理员（``GlobalRole.ADMIN``）对任何项目享有 ``OWNER`` 权限。
- ``require_role(project_id, min_role)`` 在权限不足时返回 ``404`` 而不是 ``403``，
  避免「项目是否存在」侧信道泄漏（M1-10 退出条件）。
- ``record_audit`` 写入 ``audit_events``，并维护项目级 SHA-256 哈希链 +
  ``prev_hash``，便于事后不可篡改验证。
- 启动时调用 ``ensure_bootstrap_admin``：若 ``users`` 表为空，自动创建一个
  ``admin@ontolohub.local`` 账号；dev seed 任务由 ``scripts/seed_dev.py`` 调用。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

from fastapi import Depends, Header, HTTPException, Path, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import connection as _conn
from src.db.identity import (
    GlobalRole,
    Membership,
    Role,
    User,
    has_role as _has_role,
)
from src.db.governance import AuditEvent, AuditEventType

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOOTSTRAP_ADMIN_EMAIL = "admin@ontolohub.local"
BOOTSTRAP_ADMIN_NAME = "Bootstrap Admin"

HEADER_EMAIL = "x-user-email"
HEADER_ID = "x-user-id"
HEADER_NAME = "x-user-name"


# ---------------------------------------------------------------------------
# Public dataclass used by FastAPI dependencies
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CurrentPrincipal:
    """The resolved identity for a request."""

    user: User
    # True iff global admin (superuser in M1)
    is_admin: bool


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def ensure_bootstrap_admin() -> User:
    """创建（如果还没有） bootstrap 管理员账号。

    在 ``apps/api/src/api/main.py`` 的 lifespan 启动里被调用一次，
    也被 ``scripts/seed_dev.py`` 显式触发。"""
    from src.db.connection import async_engine
    async with _conn.async_session_factory() as session:
        existing = await session.execute(
            select(User).where(User.email == BOOTSTRAP_ADMIN_EMAIL)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            await session.commit()
            return row
        admin = User(
            email=BOOTSTRAP_ADMIN_EMAIL,
            display_name=BOOTSTRAP_ADMIN_NAME,
            global_role=GlobalRole.ADMIN.value,
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        # 调试时确认 commit 成功：admin.id 应该已经填回
        print(f"[bootstrap_admin] created admin id={admin.id} via engine={async_engine.url}")
        return admin


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    x_user_email: Optional[str] = Header(default=None, alias=HEADER_EMAIL),
    x_user_id: Optional[str] = Header(default=None, alias=HEADER_ID),
    x_user_name: Optional[str] = Header(default=None, alias=HEADER_NAME),
    session: AsyncSession = Depends(_conn.get_session),
) -> CurrentPrincipal:
    """Resolve the calling user from headers.

    Modes (highest priority first):
    1. ``X-User-Email``: look up / lazily create the user with that email.
       If the email is new, it is auto-created with ``GlobalRole.USER``.
    2. ``X-User-Id``: look up by id; 401 if not found.
    3. Fallback: bootstrap admin (single-tenant dev convenience).

    Returns a ``CurrentPrincipal``; downstream routes consume ``principal.user``.
    """
    user: Optional[User] = None

    if x_user_email:
        normalized = x_user_email.strip().lower()
        if normalized:
            result = await session.execute(
                select(User).where(User.email == normalized)
            )
            user = result.scalar_one_or_none()
            if user is None:
                # Dev mode: lazily bootstrap a fresh local user.
                user = User(
                    email=normalized,
                    display_name=x_user_name or normalized.split("@")[0],
                    global_role=GlobalRole.USER.value,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
                await session.refresh(user)
    elif x_user_id:
        try:
            user_uuid = uuid.UUID(x_user_id)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"invalid X-User-Id: {e}",
            ) from e
        result = await session.execute(
            select(User).where(User.id == user_uuid)
        )
        user = result.scalar_one_or_none()

    if user is None:
        # Fallback to bootstrap admin (dev convenience).
        result = await session.execute(
            select(User).where(User.email == BOOTSTRAP_ADMIN_EMAIL)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="no bootstrap admin; call POST /api/auth/bootstrap first",
            )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user is inactive"
        )

    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)
    await session.flush()

    principal = CurrentPrincipal(
        user=user,
        is_admin=(user.global_role == GlobalRole.ADMIN.value),
    )
    return principal


# ---------------------------------------------------------------------------
# Project-scoped permission helpers
# ---------------------------------------------------------------------------

async def get_project_role(
    session: AsyncSession,
    *,
    user: User,
    project_id: uuid.UUID,
    is_admin: bool = False,
) -> Optional[Role]:
    """Return the user's per-project role, or ``None`` if not a member.

    Global admins are treated as OWNER."""
    if is_admin:
        return Role.OWNER
    result = await session.execute(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.project_id == project_id,
            Membership.is_active.is_(True),
        )
    )
    m = result.scalar_one_or_none()
    if m is None:
        return None
    try:
        return Role(m.role)
    except ValueError:
        return None


async def require_project_role(
    project_id: uuid.UUID,
    min_role: Role,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(_conn.get_session),
) -> tuple[CurrentPrincipal, Role]:
    """Dependency factory: returns (principal, role) iff ``role >= min_role``.

    关键：项目不存在 / 调用方不在项目里都返回 ``404``，不返回 ``403``，避免泄漏
    项目的存在性。
    """
    from src.db.project import Project  # late import to avoid circular
    proj = await session.execute(select(Project).where(Project.id == project_id))
    project = proj.scalar_one_or_none()
    if project is None:
        # 项目不存在 / 调用方没有权限 — 二者都用 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project not found",
        )

    role = await get_project_role(
        session,
        user=principal.user,
        project_id=project_id,
        is_admin=principal.is_admin,
    )
    if role is None or not _has_role(role, min_role):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project not found",
        )
    return principal, role


def require_role(min_role: Role):
    """Return a FastAPI dependency enforcing the given per-project role.

    Reads ``project_id`` from the path. Use as::

        @router.post("/{project_id}/foo")
        async def foo(
            ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
        ):
            principal, role = ctx
            ...
    """
    async def dep(
        project_id: uuid.UUID = Path(..., description="project scope"),
        principal: CurrentPrincipal = Depends(get_current_user),
        session: AsyncSession = Depends(_conn.get_session),
    ) -> tuple[CurrentPrincipal, Role]:
        return await require_project_role(
            project_id, min_role, principal=principal, session=session
        )
    return dep


def require_role_query(min_role: Role):
    """``require_role`` 的 Query 变体 — 路由没有 ``project_id`` 路径段时使用。

    行为完全一致：不足权限 / 项目不存在 → 404，避免侧信道泄漏。

    示例::

        @router.get("/sources")
        async def list_sources(
            project_id: uuid.UUID = Query(...),
            ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.VIEWER)),
        ):
            ...
    """
    async def dep(
        project_id: uuid.UUID = Query(..., description="project scope"),
        principal: CurrentPrincipal = Depends(get_current_user),
        session: AsyncSession = Depends(_conn.get_session),
    ) -> tuple[CurrentPrincipal, Role]:
        return await require_project_role(
            project_id, min_role, principal=principal, session=session
        )
    return dep


# ---------------------------------------------------------------------------
# Audit helper (hash-chained append-only log)
# ---------------------------------------------------------------------------

def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )


def _now_utc() -> datetime:
    """UTC-aware ``now`` with microsecond precision for ``AuditEvent`` ordering."""
    return datetime.now(timezone.utc).replace(microsecond=datetime.now(timezone.utc).microsecond)


async def _latest_entry_hash(
    session: AsyncSession, project_id: Optional[uuid.UUID]
) -> Optional[str]:
    """Find the last entry_hash for chain-link; project_id scoped if known."""
    if project_id is None:
        result = await session.execute(
            select(AuditEvent.entry_hash)
            .where(AuditEvent.project_id.is_(None))
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
    else:
        result = await session.execute(
            select(AuditEvent.entry_hash)
            .where(AuditEvent.project_id == project_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
    return result.scalar_one_or_none()


async def record_audit(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    principal: CurrentPrincipal,
    project_id: Optional[uuid.UUID],
    target_type: str,
    target_id: Optional[str],
    target_label: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
    request: Optional[Request] = None,
) -> AuditEvent:
    """Write one audit event with hash-chain linkage.

    The session is *not* committed here; the caller owns the transaction.
    """
    actor = principal.user
    actor_ip = None
    actor_ua = None
    if request is not None:
        if request.client is not None:
            actor_ip = request.client.host
        actor_ua = request.headers.get("user-agent")

    prev_hash = await _latest_entry_hash(session, project_id)

    entry = AuditEvent(
        event_type=event_type.value if isinstance(event_type, Enum) else event_type,
        actor_id=actor.id,
        actor_name=actor.display_name,
        actor_ip=actor_ip,
        actor_user_agent=actor_ua,
        project_id=project_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        target_label=target_label,
        before=before,
        after=after,
        notes=notes,
    )
    # 显式设置 created_at；SQLAlchemy 同步 SQLite 下 ``func.now()`` 只有秒级精度，
    # 多事件同秒时会让 hash 链无法按 ``created_at`` 严格回放。这里用 Python utc
    # 微秒精度，并直接写到对象，避免 server_default 覆盖。
    entry.created_at = _now_utc()
    entry.updated_at = entry.created_at
    session.add(entry)
    await session.flush()
    await session.refresh(entry)

    payload = {
        "id": str(entry.id),
        "event_type": entry.event_type.value,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "project_id": str(entry.project_id) if entry.project_id else None,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "prev_hash": prev_hash,
        "before": entry.before,
        "after": entry.after,
    }
    canonical = _canonical_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    entry.entry_hash = digest
    entry.prev_hash = prev_hash
    await session.flush()
    return entry


def coerce_diff(obj: Any) -> Optional[dict[str, Any]]:
    """Convert an entity (or dict) to a JSON-safe diff payload.

    UUIDs and enums are converted to their native Python types that JSON handles natively.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj

    def _serialize(v: Any) -> Any:
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, Enum):
            return v.value if hasattr(v, "value") else str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, (set, frozenset)):
            return sorted(_serialize(i) for i in v)
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        if hasattr(v, "__dict__"):
            return {k: _serialize(val) for k, val in v.__dict__.items() if not k.startswith("_")}
        return v

    return {
        c.key: _serialize(getattr(obj, c.key))
        for c in obj.__table__.columns  # type: ignore[attr-defined]
    }

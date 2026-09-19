"""Admin-only auth helper for system-wide endpoints (HIA-90 D5).

The existing ``require_role(Role.ADMIN)`` in :mod:`src.api.auth` is
project-scoped (it expects ``project_id`` in the path).  Backup &
DR endpoints are *system-wide* — they should be gated by
``GlobalRole.ADMIN`` only, never project membership.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import GlobalRole, User
from .auth import CurrentPrincipal, get_current_user


async def require_global_admin(
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Dependency: return the caller's ``User`` only if they are global admin.

    Non-admin callers get 403 — we are OK to leak the existence of the
    admin surface here because the endpoint itself is admin-only by
    design.
    """
    if principal.is_admin:
        return principal.user
    # Belt-and-braces: re-check the live row in case the principal was
    # constructed from a stale global_role.
    fresh = (await session.execute(
        select(User).where(User.id == principal.user.id)
    )).scalar_one_or_none()
    if fresh is None or fresh.global_role != GlobalRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="global admin required",
        )
    return fresh

"""审计追溯 API（HIA-51 M1-01）。

- ``GET /projects/{project_id}/audit``   列出项目内审计事件（含全局 hash 链状态）
- ``GET /audit/{event_id}``              单条审计事件 + before/after 详情
- ``GET /audit/verify``                  全平台哈希链校验（耗时长，管理员用）
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import Role
from src.db.governance import AuditEvent, AuditEventType
from src.api.auth import (
    CurrentPrincipal,
    _canonical_json,
    get_current_user,
    require_role,
)

router = APIRouter(tags=["audit"])


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    actor_id: Optional[uuid.UUID] = None
    actor_name: Optional[str] = None
    actor_ip: Optional[str] = None
    project_id: Optional[uuid.UUID] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    target_label: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
    prev_hash: Optional[str] = None
    entry_hash: Optional[str] = None
    created_at: str
    notes: Optional[str] = None

    model_config = {"from_attributes": True}


@router.get(
    "/projects/{project_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_project_audit(
    project_id: uuid.UUID = Path(..., description="project scope"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    event_type: Optional[AuditEventType] = Query(None),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[AuditEventResponse]:
    """List audit events for a project (newest first)."""
    query = select(AuditEvent).where(AuditEvent.project_id == project_id)
    if event_type is not None:
        query = query.where(AuditEvent.event_type == event_type.value)
    query = query.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(query)
    return [_to_response(e) for e in result.scalars().all()]


@router.get("/audit/verify")
async def verify_chain(
    project_id: Optional[uuid.UUID] = Query(None),
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Walk the hash chain for one project (or all global events if pid=None).

    Returns ``{"ok": bool, "broken_at": id|None, "checked": int}``.
    """
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    query = select(AuditEvent).order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
    if project_id is not None:
        query = query.where(AuditEvent.project_id == project_id)
    else:
        query = query.where(AuditEvent.project_id.is_(None))
    result = await session.execute(query)
    events = result.scalars().all()

    prev_hash: Optional[str] = None
    checked = 0
    for e in events:
        payload = {
            "id": str(e.id),
            "event_type": e.event_type.value if hasattr(e.event_type, "value") else e.event_type,
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "project_id": str(e.project_id) if e.project_id else None,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "prev_hash": prev_hash,
            "before": e.before,
            "after": e.after,
        }
        digest = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        if e.prev_hash != prev_hash or e.entry_hash != digest:
            return {"ok": False, "broken_at": str(e.id), "checked": checked}
        prev_hash = e.entry_hash
        checked += 1
    return {"ok": True, "broken_at": None, "checked": checked}


@router.get("/audit/{event_id}", response_model=AuditEventResponse)
async def get_audit_event(
    event_id: uuid.UUID = Path(..., description="audit event id"),
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AuditEventResponse:
    """Fetch one audit event by id. Requires global admin (single-tenant M1)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.id == event_id)
    )
    e = result.scalar_one_or_none()
    if e is None:
        raise HTTPException(status_code=404, detail="audit event not found")
    return _to_response(e)


def _to_response(e: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=e.id,
        event_type=e.event_type.value if hasattr(e.event_type, "value") else e.event_type,
        actor_id=e.actor_id,
        actor_name=e.actor_name,
        actor_ip=e.actor_ip,
        project_id=e.project_id,
        target_type=e.target_type,
        target_id=e.target_id,
        target_label=e.target_label,
        before=e.before,
        after=e.after,
        prev_hash=e.prev_hash,
        entry_hash=e.entry_hash,
        created_at=e.created_at.isoformat() if e.created_at else "",
        notes=e.notes,
    )

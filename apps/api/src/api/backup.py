"""Backup & DR API — HIA-90 D5.

All endpoints are admin-only — only members with the OWNER role on a
*system* workspace (``System`` workspace, see ``workspace.py``) may
operate backups.  In the M3 single-tenant default we accept any user
who passes ``require_role(Role.ADMIN)`` (project-level) — the
multi-tenant isolation for backups is configured per-deployment.

Endpoints:

  POST   /api/admin/backups                   — trigger a backup
  GET    /api/admin/backups                   — list (filter by kind/status)
  GET    /api/admin/backups/{id}              — detail
  DELETE /api/admin/backups/{id}              — delete (purge blobs + row)
  POST   /api/admin/backups/{id}/verify       — integrity check
  POST   /api/admin/backups/{id}/restore      — dry-run by default
  POST   /api/admin/backups/expire            — run expiry sweep
  GET    /api/admin/backup-schedules          — list schedules
  POST   /api/admin/dr-drill                  — start a DR drill
  GET    /api/admin/dr-drills                 — list past drills
  GET    /api/admin/health/backup             — last-success age metric
"""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.backup import (
    Backup,
    BackupKind,
    BackupSchedule,
    BackupStatus,
    DrDrill,
    DrDrillStatus,
    RestoreLog,
    RestoreStatus,
    ScheduleKind,
)
from src.db.governance import AuditEventType
from src.db.identity import User
from src.api.auth import (
    CurrentPrincipal,
    coerce_diff,
    record_audit,
)
from src.api.auth_admin import require_global_admin

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin", tags=["admin-backup"])


# =====================================================================
# Pydantic
# =====================================================================


class BackupCreateRequest(BaseModel):
    kind: BackupKind = BackupKind.FULL
    retention_days: Optional[int] = Field(None, ge=1, le=3650)
    notes: Optional[str] = None
    components: Optional[list[str]] = Field(
        None,
        description="Subset of components (only used with kind=full): "
        "['database', 'files', 'config']",
    )
    parent_backup_id: Optional[uuid.UUID] = None


class BackupResponse(BaseModel):
    id: uuid.UUID
    kind: BackupKind
    status: BackupStatus
    storage_path: str
    storage_backend: str
    size_bytes: int
    checksum: Optional[str]
    is_encrypted: bool
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    retention_days: int
    expires_at: Optional[str]
    triggered_by: str
    triggered_by_user: Optional[uuid.UUID]
    parent_backup_id: Optional[uuid.UUID]
    error_message: Optional[str]
    metadata: Optional[dict] = None
    created_at: str

    model_config = {"from_attributes": True}


class RestoreRequest(BaseModel):
    target_dir: Optional[str] = None
    components: Optional[list[str]] = None
    dry_run: bool = True


class RestoreLogResponse(BaseModel):
    id: uuid.UUID
    backup_id: uuid.UUID
    status: RestoreStatus
    dry_run: bool
    components: Optional[list]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    error_message: Optional[str]
    performed_by_label: str
    created_at: str

    model_config = {"from_attributes": True}


class DrDrillResponse(BaseModel):
    id: uuid.UUID
    backup_id: Optional[uuid.UUID]
    restore_log_id: Optional[uuid.UUID]
    status: DrDrillStatus
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    rto_seconds: Optional[float]
    rpo_seconds: Optional[float]
    checklist: Optional[dict]
    report: Optional[dict]
    error_message: Optional[str]
    triggered_by: str
    created_at: str

    model_config = {"from_attributes": True}


class BackupScheduleResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: ScheduleKind
    cron_expression: str
    enabled: bool
    retention_days: int
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    last_backup_id: Optional[uuid.UUID]

    model_config = {"from_attributes": True}


class BackupHealthResponse(BaseModel):
    last_success_at: Optional[str]
    last_success_age_seconds: Optional[float]
    last_success_age_human: Optional[str]
    pending_running: int
    failed_last_24h: int
    oldest_pending_seconds: Optional[float]
    threshold_seconds: int
    healthy: bool
    note: str


# =====================================================================
# Converters
# =====================================================================


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def _backup_to_response(b: Backup) -> BackupResponse:
    return BackupResponse(
        id=b.id,
        kind=b.kind,
        status=b.status,
        storage_path=b.storage_path,
        storage_backend=b.storage_backend,
        size_bytes=b.size_bytes,
        checksum=b.checksum,
        is_encrypted=b.is_encrypted,
        started_at=_iso(b.started_at),
        completed_at=_iso(b.completed_at),
        duration_ms=b.duration_ms,
        retention_days=b.retention_days,
        expires_at=_iso(b.expires_at),
        triggered_by=b.triggered_by,
        triggered_by_user=b.triggered_by_user,
        parent_backup_id=b.parent_backup_id,
        error_message=b.error_message,
        metadata=b.backup_metadata,
        created_at=_iso(b.created_at),
    )


def _restore_to_response(r: RestoreLog) -> RestoreLogResponse:
    return RestoreLogResponse(
        id=r.id,
        backup_id=r.backup_id,
        status=r.status,
        dry_run=r.dry_run,
        components=r.components,
        started_at=_iso(r.started_at),
        completed_at=_iso(r.completed_at),
        duration_ms=r.duration_ms,
        error_message=r.error_message,
        performed_by_label=r.performed_by_label,
        created_at=_iso(r.created_at),
    )


def _drill_to_response(d: DrDrill) -> DrDrillResponse:
    return DrDrillResponse(
        id=d.id,
        backup_id=d.backup_id,
        restore_log_id=d.restore_log_id,
        status=d.status,
        started_at=_iso(d.started_at),
        completed_at=_iso(d.completed_at),
        duration_ms=d.duration_ms,
        rto_seconds=d.rto_seconds,
        rpo_seconds=d.rpo_seconds,
        checklist=d.checklist,
        report=d.report,
        error_message=d.error_message,
        triggered_by=d.triggered_by,
        created_at=_iso(d.created_at),
    )


def _schedule_to_response(s: BackupSchedule) -> BackupScheduleResponse:
    return BackupScheduleResponse(
        id=s.id,
        name=s.name,
        kind=s.kind,
        cron_expression=s.cron_expression,
        enabled=s.enabled,
        retention_days=s.retention_days,
        last_run_at=_iso(s.last_run_at),
        next_run_at=_iso(s.next_run_at),
        last_backup_id=s.last_backup_id,
    )


# =====================================================================
# Helpers
# =====================================================================


async def _resolve_actor_id(user) -> Optional[uuid.UUID]:
    if hasattr(user, "user"):
        return user.user.id
    if hasattr(user, "id"):
        return user.id
    return None


# =====================================================================
# Backup CRUD
# =====================================================================


@router.post(
    "/backups",
    response_model=BackupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_backup(
    data: BackupCreateRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_global_admin),
) -> BackupResponse:
    """Trigger a backup synchronously (returns after success/failure)."""
    from src.services.backup import BackupManager

    actor_id = await _resolve_actor_id(user)
    manager = BackupManager()
    try:
        backup = await manager.create_backup(
            session,
            data.kind,
            triggered_by="manual",
            triggered_by_user=actor_id,
            retention_days=data.retention_days,
            notes=data.notes,
            parent_backup_id=data.parent_backup_id,
            components=data.components,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"backup failed: {exc}",
        )
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=None,
        target_type="backup",
        target_id=str(backup.id),
        after={
            "kind": backup.kind.value,
            "status": backup.status.value,
            "size_bytes": backup.size_bytes,
        },
    )
    return _backup_to_response(backup)


@router.get("/backups", response_model=list[BackupResponse])
async def list_backups(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
    kind: Optional[BackupKind] = None,
    status_filter: Optional[BackupStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[BackupResponse]:
    q = select(Backup).order_by(Backup.created_at.desc()).limit(limit).offset(offset)
    if kind is not None:
        q = q.where(Backup.kind == kind)
    if status_filter is not None:
        q = q.where(Backup.status == status_filter)
    rows = (await session.execute(q)).scalars().all()
    return [_backup_to_response(b) for b in rows]


@router.get("/backups/{backup_id}", response_model=BackupResponse)
async def get_backup(
    backup_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
) -> BackupResponse:
    row = (await session.execute(
        select(Backup).where(Backup.id == backup_id),
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="backup not found")
    return _backup_to_response(row)


@router.delete(
    "/backups/{backup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_backup(
    backup_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_global_admin),
) -> None:
    row = (await session.execute(
        select(Backup).where(Backup.id == backup_id),
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="backup not found")
    # Wipe blobs first; failures here are recorded but do not block the row delete
    blob_path = Path(row.storage_path)
    wiped = False
    try:
        if blob_path.exists():
            if blob_path.is_dir():
                shutil.rmtree(blob_path, ignore_errors=True)
            else:
                blob_path.unlink(missing_ok=True)
            wiped = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to wipe backup blobs at %s: %s", blob_path, exc)
    await session.delete(row)
    await session.flush()
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=None,
        target_type="backup",
        target_id=str(backup_id),
        after={"blobs_wiped": wiped},
    )


@router.post("/backups/{backup_id}/verify")
async def verify_backup(
    backup_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
) -> dict:
    from src.services.backup import BackupManager, BackupNotFound
    manager = BackupManager()
    try:
        ok, report = await manager.verify_backup(session, backup_id)
    except BackupNotFound:
        raise HTTPException(status_code=404, detail="backup not found")
    return {"ok": ok, "report": report}


@router.post(
    "/backups/{backup_id}/restore",
    response_model=RestoreLogResponse,
)
async def restore_backup(
    backup_id: uuid.UUID,
    data: RestoreRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_global_admin),
) -> RestoreLogResponse:
    from src.services.backup import BackupManager, BackupNotFound
    actor_id = await _resolve_actor_id(user)
    manager = BackupManager()
    target = Path(data.target_dir) if data.target_dir else None
    try:
        log = await manager.restore_backup(
            session,
            backup_id,
            target_dir=target,
            components=data.components,
            dry_run=data.dry_run,
            performed_by=actor_id,
            performed_by_label="manual",
        )
    except BackupNotFound:
        raise HTTPException(status_code=404, detail="backup not found")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"restore failed: {exc}")
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=None,
        target_type="backup_restore",
        target_id=str(log.id),
        after={
            "backup_id": str(backup_id),
            "dry_run": data.dry_run,
            "components": data.components,
        },
    )
    return _restore_to_response(log)


@router.post("/backups/expire")
async def run_expire(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
) -> dict:
    from src.services.backup import BackupManager
    manager = BackupManager()
    expired = await manager.expire_old_backups(session)
    return {"expired_ids": [str(i) for i in expired], "count": len(expired)}


# =====================================================================
# Schedules
# =====================================================================


@router.get("/backup-schedules", response_model=list[BackupScheduleResponse])
async def list_schedules(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
) -> list[BackupScheduleResponse]:
    from src.services.backup_scheduler import ensure_default_schedules
    await ensure_default_schedules(session)
    rows = (await session.execute(
        select(BackupSchedule).order_by(BackupSchedule.name),
    )).scalars().all()
    return [_schedule_to_response(s) for s in rows]


# =====================================================================
# DR drill
# =====================================================================


@router.post(
    "/dr-drill",
    response_model=DrDrillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_dr_drill(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_global_admin),
    backup_id: Optional[uuid.UUID] = None,
) -> DrDrillResponse:
    from src.services.backup import BackupManager
    actor_id = await _resolve_actor_id(user)
    manager = BackupManager()
    try:
        drill = await manager.run_dr_drill(
            session,
            backup_id=backup_id,
            performed_by=actor_id,
            triggered_by="manual",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"dr-drill failed: {exc}")
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=None,
        target_type="dr_drill",
        target_id=str(drill.id),
        after={
            "backup_id": str(drill.backup_id) if drill.backup_id else None,
            "status": drill.status.value,
        },
    )
    return _drill_to_response(drill)


@router.get("/dr-drills", response_model=list[DrDrillResponse])
async def list_dr_drills(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
    limit: int = Query(20, ge=1, le=200),
) -> list[DrDrillResponse]:
    rows = (await session.execute(
        select(DrDrill).order_by(DrDrill.started_at.desc()).limit(limit),
    )).scalars().all()
    return [_drill_to_response(d) for d in rows]


# =====================================================================
# Health
# =====================================================================


@router.get("/health/backup", response_model=BackupHealthResponse)
async def backup_health(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_global_admin),
) -> BackupHealthResponse:
    """Operational health view of the backup subsystem."""
    from src.core.config import settings

    last_success = (await session.execute(
        select(Backup)
        .where(Backup.status == BackupStatus.SUCCESS)
        .order_by(Backup.completed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    pending_running = (await session.execute(
        select(func.count(Backup.id)).where(
            Backup.status.in_([BackupStatus.PENDING, BackupStatus.RUNNING]),
        )
    )).scalar_one()
    cutoff_24h = datetime.now(timezone.utc).timestamp() - 86400
    failed_24h = (await session.execute(
        select(func.count(Backup.id)).where(
            Backup.status == BackupStatus.FAILED,
            Backup.created_at >= datetime.fromtimestamp(cutoff_24h, tz=timezone.utc),
        )
    )).scalar_one()

    last_success_at = last_success.completed_at if last_success else None
    # SQLite 不存 timezone 信息，读回的 datetime 是 naive。这里如果是
    # naive 就当 UTC 处理，避免与 ``datetime.now(timezone.utc)`` 比较
    # 时抛 ``TypeError: can't subtract offset-naive and offset-aware``。
    if last_success_at is not None and last_success_at.tzinfo is None:
        last_success_at = last_success_at.replace(tzinfo=timezone.utc)
    age = (
        (datetime.now(timezone.utc) - last_success_at).total_seconds()
        if last_success_at else None
    )
    human = (
        f"{int(age // 86400)}d {int((age % 86400) // 3600)}h"
        if age is not None else None
    )
    threshold = settings.backup.max_backup_age_seconds
    healthy = age is not None and age < threshold and failed_24h == 0

    if healthy:
        note = "OK"
    elif age is None:
        note = "no successful backup ever recorded"
    elif age >= threshold:
        note = (
            f"last successful backup is older than threshold "
            f"({int(age)}s > {threshold}s)"
        )
    else:
        note = f"{failed_24h} failed backup(s) in the last 24h"

    return BackupHealthResponse(
        last_success_at=_iso(last_success_at),
        last_success_age_seconds=age,
        last_success_age_human=human,
        pending_running=int(pending_running or 0),
        failed_last_24h=int(failed_24h or 0),
        oldest_pending_seconds=None,
        threshold_seconds=threshold,
        healthy=healthy,
        note=note,
    )

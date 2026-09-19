"""Backup scheduler — HIA-90 D5.

Implements a tiny cron-style scheduler that hooks into the existing
``_cron_loop`` in ``src.api.main``.  For each enabled ``BackupSchedule``
whose ``next_run_at`` has passed we run the corresponding backup kind.

We deliberately avoid APScheduler/Celery to stay consistent with the
M2 webhook scheduler (HIA-75 C3): the cron loop runs in-process and
fires every 60s.  This is good enough for daily full / 6h incremental
cadences and trivially testable.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.backup import (
    Backup,
    BackupKind,
    BackupSchedule,
    ScheduleKind,
)
from src.db.connection import get_session
from src.services.backup import BackupManager, BackupError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_fire(cron_expression: str, after: datetime) -> datetime:
    return croniter(cron_expression, after).get_next(datetime)


async def ensure_default_schedules(session: AsyncSession) -> None:
    """Create the two default schedules (daily full + 6h incremental) if absent."""
    from src.core.config import settings

    defaults = [
        {
            "name": "daily-full",
            "kind": ScheduleKind.FULL,
            "cron_expression": settings.backup.full_schedule_cron,
            "retention_days": settings.backup.full_retention_days,
            "enabled": settings.backup.schedule_enabled,
        },
        {
            "name": "every-6h-incremental",
            "kind": ScheduleKind.INCREMENTAL,
            "cron_expression": settings.backup.incremental_schedule_cron,
            "retention_days": settings.backup.incremental_retention_days,
            "enabled": settings.backup.schedule_enabled,
        },
    ]

    for cfg in defaults:
        existing = (await session.execute(
            select(BackupSchedule).where(BackupSchedule.name == cfg["name"]),
        )).scalar_one_or_none()
        if existing:
            continue
        sched = BackupSchedule(
            name=cfg["name"],
            kind=cfg["kind"],
            cron_expression=cfg["cron_expression"],
            retention_days=cfg["retention_days"],
            enabled=cfg["enabled"],
            next_run_at=_next_fire(
                cfg["cron_expression"], datetime.now(timezone.utc),
            ),
        )
        session.add(sched)
    await session.flush()


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def run_backup_schedules() -> dict:
    """One scheduler tick — called from the main lifespan cron loop.

    Returns a small summary dict so callers / tests can assert behaviour.
    """
    from src.core.config import settings
    if not settings.backup.schedule_enabled:
        return {"skipped": "disabled"}

    fired: list[dict] = []
    async for session in get_session():
        await ensure_default_schedules(session)
        now = datetime.now(timezone.utc)
        rows = (await session.execute(
            select(BackupSchedule)
            .where(
                BackupSchedule.enabled.is_(True),
                BackupSchedule.next_run_at.is_not(None),
                BackupSchedule.next_run_at <= now,
            )
        )).scalars().all()

        for sched in rows:
            try:
                manager = BackupManager()
                if sched.kind == ScheduleKind.FULL:
                    backup = await manager.create_backup(
                        session,
                        BackupKind.FULL,
                        triggered_by="schedule",
                        retention_days=sched.retention_days,
                    )
                elif sched.kind == ScheduleKind.INCREMENTAL:
                    parent_id = await _last_full_backup(session)
                    backup = await manager.create_backup(
                        session,
                        BackupKind.INCREMENTAL,
                        triggered_by="schedule",
                        retention_days=sched.retention_days,
                        parent_backup_id=parent_id,
                    )
                elif sched.kind == ScheduleKind.DRILL:
                    from src.services.backup import BackupManager as _BM
                    drill = await _BM().run_dr_drill(
                        session, triggered_by="schedule",
                    )
                    fired.append({
                        "schedule": sched.name,
                        "kind": sched.kind.value,
                        "drill_id": str(drill.id),
                    })
                    sched.last_run_at = now
                    sched.next_run_at = _next_fire(sched.cron_expression, now)
                    continue
                else:
                    logger.warning("unknown schedule kind: %s", sched.kind)
                    continue

                sched.last_run_at = now
                sched.last_backup_id = backup.id
                sched.next_run_at = _next_fire(sched.cron_expression, now)
                fired.append({
                    "schedule": sched.name,
                    "kind": sched.kind.value,
                    "backup_id": str(backup.id),
                    "status": backup.status.value,
                })
            except BackupError as exc:
                logger.warning("schedule %s failed: %s", sched.name, exc)
                fired.append({
                    "schedule": sched.name,
                    "kind": sched.kind.value,
                    "error": str(exc),
                })
                # Push next_run out by an hour to avoid hot-looping a
                # broken schedule; croniter naturally re-aligns from there.
                sched.next_run_at = _next_fire(sched.cron_expression, now)
        await session.commit()
        return {"fired": fired}


async def _last_full_backup(session: AsyncSession) -> Optional[uuid.UUID]:
    row = (await session.execute(
        select(Backup.id)
        .where(
            Backup.kind == BackupKind.FULL,
            Backup.status == Backup.status.type.enum_class.SUCCESS
            if hasattr(Backup.status.type, "enum_class") else Backup.status.in_(
                ["success"],
            ),
        )
        .order_by(Backup.completed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    return row

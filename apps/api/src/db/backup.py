"""Backup & disaster-recovery models — HIA-90 D5.

Four tables support the end-to-end backup/restore/verify flow:

* ``backups`` — one row per backup artefact produced (full / incremental /
  database / files / config).  Immutable: once ``status='success'`` the
  row never changes (except for retention expiry).
* ``restore_logs`` — every restore attempt (manual or drill).  Append-only.
* ``backup_schedules`` — cron-style schedule entries that the scheduler
  loop fires.  May be paused (enabled=false) without losing history.
* ``dr_drills`` — record of a full DR drill: pick the most recent good
  backup, restore into an isolated target, run smoke checks, capture
  RTO / RPO.  Used by ops to satisfy audit (SOC2 CC7 / GDPR art. 32).

All four reuse ``UUIDMixin`` / ``TimestampMixin`` so they slot into the
existing ``Base`` metadata without surprises.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Enum as SQLEnum,
    Index,
    JSON,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin


# =====================================================================
# Enums
# =====================================================================


class BackupKind(str, Enum):
    """What a backup contains."""

    FULL = "full"             # db + files + config
    DATABASE = "database"     # only the SQL DB
    FILES = "files"           # only data/uploads + exports + snapshots
    CONFIG = "config"         # alembic + workspace config + env-overlay
    INCREMENTAL = "incremental"  # WAL / file diff against parent_backup_id


class BackupStatus(str, Enum):
    """Lifecycle of a backup."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"        # retention expired (kept as audit record)
    CORRUPTED = "corrupted"    # verify failed


class RestoreStatus(str, Enum):
    """Lifecycle of a restore attempt."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"  # smoke checks failed; reverted


class ScheduleKind(str, Enum):
    """What the schedule triggers."""

    FULL = "full"
    INCREMENTAL = "incremental"
    DRILL = "drill"


class DrDrillStatus(str, Enum):
    """Lifecycle of a DR drill."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# =====================================================================
# Backup
# =====================================================================


class Backup(Base, UUIDMixin, TimestampMixin):
    """A single backup artefact.

    ``storage_path`` points at a local tarball (optionally encrypted).
    ``checksum`` is a sha256 hex digest of the *unencrypted* bytes; we
    verify it after decryption so corruption inside the encrypted blob
    is caught before the user sees a success.
    """

    __tablename__ = "backups"

    kind: Mapped[BackupKind] = mapped_column(
        SQLEnum(BackupKind), nullable=False, index=True,
    )
    status: Mapped[BackupStatus] = mapped_column(
        SQLEnum(BackupStatus),
        default=BackupStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Storage
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    storage_backend: Mapped[str] = mapped_column(
        String(50), default="local", nullable=False,
    )  # local | s3
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Lifecycle
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Audit
    triggered_by: Mapped[str] = mapped_column(
        String(50), default="manual", nullable=False,
    )  # manual | schedule | drill
    triggered_by_user: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Incremental chain
    parent_backup_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backups.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Free-form metadata (component counts, file list digest, etc.)
    backup_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSON, nullable=True,
    )

    __table_args__ = (
        Index("ix_backups_status_created", "status", "created_at"),
        Index("ix_backups_kind_status", "kind", "status"),
        Index("ix_backups_expires", "expires_at"),
    )

    parent: Mapped[Optional["Backup"]] = relationship(
        "Backup", remote_side="Backup.id", foreign_keys=[parent_backup_id],
    )
    restore_logs: Mapped[list["RestoreLog"]] = relationship(
        "RestoreLog", back_populates="backup", cascade="all, delete-orphan",
    )


# =====================================================================
# RestoreLog
# =====================================================================


class RestoreLog(Base, UUIDMixin, TimestampMixin):
    """Audit record of every restore attempt (manual or DR drill)."""

    __tablename__ = "restore_logs"

    backup_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[RestoreStatus] = mapped_column(
        SQLEnum(RestoreStatus),
        default=RestoreStatus.PENDING,
        nullable=False,
    )

    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    components: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # e.g. ["database", "files", "config"]; partial restore is supported

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    restored_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    performed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    performed_by_label: Mapped[str] = mapped_column(
        String(100), default="manual", nullable=False,
    )  # manual | drill | restore-script

    __table_args__ = (
        Index("ix_restore_logs_backup_status", "backup_id", "status"),
    )

    backup: Mapped["Backup"] = relationship("Backup", back_populates="restore_logs")


# =====================================================================
# BackupSchedule
# =====================================================================


class BackupSchedule(Base, UUIDMixin, TimestampMixin):
    """Cron-style schedule for the scheduler loop."""

    __tablename__ = "backup_schedules"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    kind: Mapped[ScheduleKind] = mapped_column(
        SQLEnum(ScheduleKind), nullable=False,
    )
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_backup_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backups.id", ondelete="SET NULL"),
        nullable=True,
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_backup_schedules_enabled_next", "enabled", "next_run_at"),
    )

    last_backup: Mapped[Optional["Backup"]] = relationship(
        "Backup", foreign_keys=[last_backup_id],
    )


# =====================================================================
# DrDrill
# =====================================================================


class DrDrill(Base, UUIDMixin, TimestampMixin):
    """A full DR drill: pick latest good backup, restore in isolation, smoke check."""

    __tablename__ = "dr_drills"

    backup_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    restore_log_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restore_logs.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[DrDrillStatus] = mapped_column(
        SQLEnum(DrDrillStatus),
        default=DrDrillStatus.PENDING,
        nullable=False,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Reported RTO/RPO (seconds). Filled at the end from the drill itself.
    rto_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rpo_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    checklist: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # e.g. {"db_ping": true, "files_listed": true, "smoke_crud": true, "errors": []}
    report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    triggered_by: Mapped[str] = mapped_column(
        String(50), default="manual", nullable=False,
    )
    performed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_dr_drills_status_started", "status", "started_at"),
    )

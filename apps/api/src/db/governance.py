"""Governance models ? plugin installation, audit events, drift, health snapshots."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, Any, TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String, Text, Boolean, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDMixin, TimestampMixin


class PluginStatus(str, Enum):
    """Lifecycle of a plugin installation."""

    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"
    UNINSTALLED = "uninstalled"


class AuditEventType(str, Enum):
    """Kind of audited event."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    LOGIN = "login"
    LOGOUT = "logout"
    EXPORT = "export"
    IMPORT = "import"


class DriftSeverity(str, Enum):
    """Severity of a drift proposal."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class HealthStatus(str, Enum):
    """System health snapshot status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class PluginInstallation(Base, UUIDMixin, TimestampMixin):
    """A plugin installed into the workspace."""

    __tablename__ = "plugin_installations"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[PluginStatus] = mapped_column(
        SQLEnum(PluginStatus),
        default=PluginStatus.INSTALLED,
        nullable=False,
    )

    manifest: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    install_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    installed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_plugin_installations_status", "status"),
    )


class AuditEvent(Base, UUIDMixin, TimestampMixin):
    """An append-only audit log entry."""

    __tablename__ = "audit_events"

    event_type: Mapped[AuditEventType] = mapped_column(
        SQLEnum(AuditEventType), nullable=False
    )

    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    actor_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    actor_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    actor_user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    # Target of the event (free-form entity reference)
    target_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_label: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Diff snapshot for change events
    before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Hash chain entry ? tamper-evident ordering
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_events_project_type", "project_id", "event_type"),
        Index("ix_audit_events_target", "target_type", "target_id"),
    )


class DriftProposal(Base, UUIDMixin, TimestampMixin):
    """A drift detection finding ? schema / data has diverged from expectations."""

    __tablename__ = "drift_proposals"

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    severity: Mapped[DriftSeverity] = mapped_column(
        SQLEnum(DriftSeverity),
        default=DriftSeverity.INFO,
        nullable=False,
    )

    source_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    suggested_fix: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="open")
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_drift_proposals_status", "status"),
        Index("ix_drift_proposals_severity", "severity"),
    )


class HealthSnapshot(Base, UUIDMixin, TimestampMixin):
    """Periodic system health snapshot for monitoring dashboards."""

    __tablename__ = "health_snapshots"

    status: Mapped[HealthStatus] = mapped_column(
        SQLEnum(HealthStatus), default=HealthStatus.HEALTHY, nullable=False
    )

    components: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_health_snapshots_status", "status"),
    )
"""Webhook and Trigger models — HIA-75 C3 Webhook/Trigger Integration."""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
    Enum as SQLEnum,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project


# =============================================================================
# Enums
# =============================================================================


class WebhookEventType(str, Enum):
    """Event types that can trigger an outbound webhook."""

    # Object lifecycle
    OBJECT_CREATED = "object.created"
    OBJECT_UPDATED = "object.updated"
    OBJECT_DELETED = "object.deleted"
    # CR workflow
    CR_CREATED = "cr.created"
    CR_SUBMITTED = "cr.submitted"
    CR_APPROVED = "cr.approved"
    CR_REJECTED = "cr.rejected"
    CR_MERGED = "cr.merged"
    CR_CLOSED = "cr.closed"
    CR_COMMENT_ADDED = "cr.comment_added"
    # Release & Deployment
    RELEASE_CREATED = "release.created"
    RELEASE_PUBLISHED = "release.published"
    DEPLOYMENT_STARTED = "deployment.started"
    DEPLOYMENT_SUCCEEDED = "deployment.succeeded"
    DEPLOYMENT_FAILED = "deployment.failed"
    DEPLOYMENT_ROLLBACK = "deployment.rollback"
    # Action
    ACTION_RUN_STARTED = "action_run.started"
    ACTION_RUN_COMPLETED = "action_run.completed"
    # Manual
    MANUAL_TRIGGER = "manual"


class WebhookDeliveryStatus(str, Enum):
    """Delivery attempt result."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    DROPPED = "dropped"  # max retries exceeded


class TriggerType(str, Enum):
    """Kind of trigger."""

    INBOUND_WEBHOOK = "inbound_webhook"
    SCHEDULE = "schedule"
    OBJECT_CHANGE = "object_change"


class TriggerStatus(str, Enum):
    """Trigger active state."""

    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


# =============================================================================
# Webhook Config (outbound)
# =============================================================================


class WebhookConfig(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """Configured outbound webhook for a project.

    When matching events fire, the dispatcher sends an HTTP POST to ``url``
    with an HMAC-SHA256 signature in ``X-OntoloHub-Signature``.
    """

    __tablename__ = "webhook_configs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Which events this webhook listens to
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Optional filter: JSONata/JSONPath expression evaluated against event payload
    filter_expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Secret for HMAC-SHA256 signature
    secret: Mapped[str] = mapped_column(String(255), nullable=False)

    # Retry config
    retry_count: Mapped[int] = mapped_column(Integer, default=3)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=60)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Statistics
    total_deliveries: Mapped[int] = mapped_column(Integer, default=0)
    failed_deliveries: Mapped[int] = mapped_column(Integer, default=0)
    last_delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_webhook_configs_project", "project_id"),
        UniqueConstraint("project_id", "name", name="uq_webhook_config_project_name"),
    )


class WebhookDelivery(Base, UUIDMixin, TimestampMixin):
    """Record of a single webhook delivery attempt (including retries)."""

    __tablename__ = "webhook_deliveries"

    webhook_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Full event payload that was (or will be) sent
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Delivery state
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        SQLEnum(WebhookDeliveryStatus, values_callable=lambda e: [m.name for m in e]),
        default=WebhookDeliveryStatus.PENDING,
        nullable=False,
    )

    # HTTP round-trip info
    http_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Retry tracking
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Related entity reference for convenience
    target_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_webhook_deliveries_config_status", "webhook_config_id", "status"),
        Index("ix_webhook_deliveries_target", "target_type", "target_id"),
    )


# =============================================================================
# Trigger Config (inbound / schedule / object-change)
# =============================================================================


class TriggerConfig(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A trigger: inbound webhook, cron schedule, or object-change → action run."""

    __tablename__ = "trigger_configs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What kind of trigger
    trigger_type: Mapped[TriggerType] = mapped_column(
        SQLEnum(TriggerType, values_callable=lambda e: [m.name for m in e]),
        nullable=False,
    )

    # For INBOUND_WEBHOOK: random token used in URL path
    # For SCHEDULE: cron expression, e.g. "*/5 * * * *"
    # For OBJECT_CHANGE: JSONata filter expression
    trigger_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # The ActionType to execute when this trigger fires
    action_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_types.id", ondelete="CASCADE"),
        nullable=True,
    )

    # HIA-76 C4: alternatively, a Workflow can be the target of a trigger.
    # Exactly one of action_type_id / workflow_id is set (enforced at API layer).
    workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Input template: dict merged with trigger event data to form ActionRun.input_data
    input_template: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[TriggerStatus] = mapped_column(
        SQLEnum(TriggerStatus, values_callable=lambda e: [m.name for m in e]),
        default=TriggerStatus.ACTIVE,
        nullable=False,
    )

    # Statistics
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_trigger_configs_project", "project_id"),
        Index("ix_trigger_configs_type_status", "trigger_type", "status"),
    )

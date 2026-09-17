"""Runtime models ? saved views, actions, automation rules, tasks."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project


class ViewType(str, Enum):
    """Kind of saved object view."""

    TABLE = "table"
    GRAPH = "graph"
    TIMELINE = "timeline"
    MAP = "map"
    DASHBOARD = "dashboard"


class ActionTypeStatus(str, Enum):
    """Lifecycle of an Action type definition."""

    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class ActionRunStatus(str, Enum):
    """Lifecycle of a single action execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"


class AutomationTrigger(str, Enum):
    """What kind of event wakes an automation rule."""

    OBJECT_CREATED = "object_created"
    OBJECT_UPDATED = "object_updated"
    OBJECT_DELETED = "object_deleted"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class TaskStatus(str, Enum):
    """Lifecycle of an internal task / todo."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELED = "canceled"


class ObjectView(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A saved query / view over objects in a project."""

    __tablename__ = "object_views"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    view_type: Mapped[ViewType] = mapped_column(
        SQLEnum(ViewType), default=ViewType.TABLE, nullable=False
    )

    query: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    columns: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    filters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sort: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_object_views_project", "project_id"),
    )


class ActionType(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A defined action template that can be invoked against objects."""

    __tablename__ = "action_types"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    kind: Mapped[str] = mapped_column(String(100), nullable=False)  # function | webhook | workflow

    status: Mapped[ActionTypeStatus] = mapped_column(
        SQLEnum(ActionTypeStatus),
        default=ActionTypeStatus.DRAFT,
        nullable=False,
    )

    parameters_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    return_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    runtime: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class ActionRun(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A single execution of an ActionType."""

    __tablename__ = "action_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    action_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[ActionRunStatus] = mapped_column(
        SQLEnum(ActionRunStatus),
        default=ActionRunStatus.PENDING,
        nullable=False,
    )

    input_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    triggered_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    automation_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_action_runs_project_status", "project_id", "status"),
    )


class AutomationRule(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """An automation rule: trigger -> condition -> action."""

    __tablename__ = "automation_rules"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    trigger: Mapped[AutomationTrigger] = mapped_column(
        SQLEnum(AutomationTrigger), nullable=False
    )
    trigger_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    condition: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    action_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    action_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class Task(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """An internal task / todo item for a project (manual review, follow-up, etc.)."""

    __tablename__ = "tasks"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus), default=TaskStatus.OPEN, nullable=False
    )

    priority: Mapped[int] = mapped_column(Integer, default=0)

    assignee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    assignee_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    related_object_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_tasks_project_status", "project_id", "status"),
    )
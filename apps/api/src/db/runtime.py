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


# HIA-70 C1: Function 表（独立可寻址的函数实体）
# ActionType 仍然可内联代码；新增 Function 允许不同 Action 共享同一段代码，
# 也允许 Function 单独被引用（workflow step / 函数测试面板）。


class FunctionLanguage(str, Enum):
    """Function 源码语言。"""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


class Function(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A reusable function definition (HIA-70 C1).

    与 ActionType.code/runtime 解耦：Function 是被命名、可复用、可独立测试
    的代码段；ActionType 可通过 ``function_id`` 引用一个 Function，
    也可继续保留内联 code（兼容老数据）。
    """

    __tablename__ = "functions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    api_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    language: Mapped[FunctionLanguage] = mapped_column(
        SQLEnum(FunctionLanguage),
        default=FunctionLanguage.PYTHON,
        nullable=False,
    )

    source_code: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 可选：与 ActionType.code 的元数据对应（签名、timeout 等）
    parameters_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    return_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        # 同 project 下 api_name 唯一
        Index(
            "uq_functions_project_api_name",
            "project_id",
            "api_name",
            unique=True,
        ),
    )


class FunctionRun(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A single test-run of a Function (HIA-70 C1 ``/test`` endpoint).

    与 ActionRun 区别：FunctionRun 不进入异步调度、每次 ``/test`` 都
    立即同步执行；用于编辑器内的「试运行」按钮。
    """

    __tablename__ = "function_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    function_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("functions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    input_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


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
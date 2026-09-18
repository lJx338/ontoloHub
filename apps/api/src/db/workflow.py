"""Workflow model — HIA-76 C4 Workflow 多步执行.

A Workflow is an ordered collection of *steps* that run sequentially.
Each step references either:
  - an ActionType (kind=function or kind=webhook) — invokes its sandbox executor
  - a WebhookConfig — sends outbound HTTP with HMAC signature
  - an object API call — creates/updates/links an Object via our objects API
  - a delay — sleeps N seconds

Steps run in declaration order; each step's output is exposed to subsequent
steps via JSONPath reference (``$prev.field`` or ``$steps.<step_id>.field``).

A ``WorkflowExecution`` tracks one run of a Workflow (status, input_context,
output). ``WorkflowStepResult`` records each step's input/output/timing.
"""
from __future__ import annotations

import uuid
from datetime import datetime
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


# ===========================================================================
# Enums
# ===========================================================================


class WorkflowStatus(str, Enum):
    """Lifecycle of a Workflow definition."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkflowExecutionStatus(str, Enum):
    """Lifecycle of a Workflow execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"


class WorkflowStepStatus(str, Enum):
    """Lifecycle of a single step within an execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowStepType(str, Enum):
    """Step type — drives dispatch in the executor."""

    FUNCTION_CALL = "function_call"   # ref = ActionType.id (kind=function)
    WEBHOOK_CALL = "webhook_call"     # ref = WebhookConfig.id
    OBJECT_API_CALL = "object_api"    # ref = Object class name; config.operation in {create,update,link}
    DELAY = "delay"                   # config.seconds (int)


# ===========================================================================
# Workflow definition
# ===========================================================================


class Workflow(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """An ordered sequence of steps.

    ``steps`` is a JSON list. Each step dict has the shape::

        {
            "id": "step-1",
            "name": "Send welcome email",
            "type": "function_call" | "webhook_call" | "object_api" | "delay",
            "ref": "<action_type_id | webhook_config_id | object_class_name>",
            "input_mapping": {"to": "$trigger.payload.email"},
            "config": {"timeout_s": 30, ...},
            "retry_policy": {"max_attempts": 3, "delay_s": 5},
            "error_handler": "stop" | "continue" | {"goto_step": "step-3"},
            "depends_on": ["step-0"],
        }
    """

    __tablename__ = "workflows"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[WorkflowStatus] = mapped_column(
        SQLEnum(WorkflowStatus, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowStatus.DRAFT,
        nullable=False,
    )

    # Ordered step list. Validated on write (see api/workflow.py).
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Default input template merged with trigger context on each execution.
    # Mirrors TriggerConfig.input_template semantics for manual executions.
    default_input: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Statistics
    total_executions: Mapped[int] = mapped_column(Integer, default=0)
    failed_executions: Mapped[int] = mapped_column(Integer, default=0)
    last_executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_workflows_project", "project_id"),
        UniqueConstraint("project_id", "name", name="uq_workflows_project_name"),
    )


# ===========================================================================
# Execution record
# ===========================================================================


class WorkflowExecution(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """One run of a Workflow."""

    __tablename__ = "workflow_executions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[WorkflowExecutionStatus] = mapped_column(
        SQLEnum(WorkflowExecutionStatus, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowExecutionStatus.PENDING,
        nullable=False,
    )

    # What woke this execution: 'manual' | 'webhook' | 'schedule' | 'object_change'
    trigger_kind: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)
    trigger_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # The merged input the executor sees (trigger context + default_input + override)
    input_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Final merged output from all steps (flat dict)
    output: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Step results — relationship, but also stored as JSON for fast list views.
    # Detailed records live in WorkflowStepResult (joined via execution_id).
    triggered_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        Index("ix_workflow_executions_project", "project_id"),
        Index("ix_workflow_executions_workflow", "workflow_id"),
        Index("ix_workflow_executions_status", "status"),
    )


class WorkflowStepResult(Base, UUIDMixin, TimestampMixin):
    """Record of one step within a WorkflowExecution."""

    __tablename__ = "workflow_step_results"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Logical step id from Workflow.steps[].id (string), and its index.
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False)
    step_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[WorkflowStepStatus] = mapped_column(
        SQLEnum(WorkflowStepStatus, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowStepStatus.PENDING,
        nullable=False,
    )

    input_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, default=1)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_workflow_step_results_exec", "execution_id"),
        Index("ix_workflow_step_results_status", "status"),
    )

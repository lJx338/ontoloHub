"""Workflow tables + trigger_configs.workflow_id (HIA-76 C4 — Action 编排)

Adds:

* ``workflows`` — Workflow definition (project-level; ordered steps list)
* ``workflow_executions`` — single run record (status, input_context, output)
* ``workflow_step_results`` — per-step record (input/output/timing)
* ``trigger_configs.workflow_id`` — optional alternative target to action_type_id

When a trigger fires:
* if ``action_type_id`` is set → create ``ActionRun`` (existing path)
* if ``workflow_id`` is set   → create ``WorkflowExecution`` (new path)
* exactly one of the two must be set (enforced in API layer, not DB)

Revision ID: 0008_workflow
Revises: 0007_webhook_trigger
Create Date: 2026-09-18 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0008_workflow"
down_revision: str | None = "0007_webhook_trigger"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    cols = {c["name"] for c in insp.get_columns(table)}
    return column in cols


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return any(idx["name"] == index_name for idx in insp.get_indexes(table))


def _create_index_safe(name: str, table: str, columns: list[str], **kw) -> None:
    if _has_index(table, name):
        return
    op.create_index(name, table, columns, **kw)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_enum_type(name: str, values: list[str]) -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})"))


def _drop_enum_type(name: str) -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # =======================================================================
    # Enum types (Postgres only)
    # =======================================================================
    _create_enum_type(
        "workflowstatus",
        ["draft", "active", "archived"],
    )
    _create_enum_type(
        "workflowexecutionstatus",
        ["pending", "running", "success", "failed", "canceled"],
    )
    _create_enum_type(
        "workflowstepstatus",
        ["pending", "running", "success", "failed", "skipped"],
    )

    # =======================================================================
    # 1. workflows — definition
    # =======================================================================
    if not _has_table("workflows"):
        op.create_table(
            "workflows",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="draft",
            ),
            sa.Column("steps", sa.JSON, nullable=False, server_default="[]"),
            sa.Column("default_input", sa.JSON, nullable=True),
            sa.Column(
                "total_executions",
                sa.Integer,
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "failed_executions",
                sa.Integer,
                nullable=False,
                server_default="0",
            ),
            sa.Column("last_executed_at", sa.DateTime(timezone=True), nullable=True),
            # TimestampMixin
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            sa.UniqueConstraint(
                "project_id", "name", name="uq_workflows_project_name"
            ),
        )
        _create_index_safe("ix_workflows_project", "workflows", ["project_id"])

    # =======================================================================
    # 2. workflow_executions
    # =======================================================================
    if not _has_table("workflow_executions"):
        op.create_table(
            "workflow_executions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "workflow_id",
                UUID(as_uuid=True),
                sa.ForeignKey("workflows.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "trigger_kind",
                sa.String(50),
                nullable=False,
                server_default="manual",
            ),
            sa.Column("trigger_id", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "input_context",
                sa.JSON,
                nullable=False,
                server_default="{}",
            ),
            sa.Column("output", sa.JSON, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("triggered_by", sa.String(100), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        _create_index_safe(
            "ix_workflow_executions_project", "workflow_executions", ["project_id"]
        )
        _create_index_safe(
            "ix_workflow_executions_workflow", "workflow_executions", ["workflow_id"]
        )
        _create_index_safe(
            "ix_workflow_executions_status", "workflow_executions", ["status"]
        )

    # =======================================================================
    # 3. workflow_step_results
    # =======================================================================
    if not _has_table("workflow_step_results"):
        op.create_table(
            "workflow_step_results",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "execution_id",
                UUID(as_uuid=True),
                sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("step_id", sa.String(255), nullable=False),
            sa.Column("step_index", sa.Integer, nullable=False),
            sa.Column("step_type", sa.String(50), nullable=False),
            sa.Column("step_name", sa.String(255), nullable=True),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("input_data", sa.JSON, nullable=True),
            sa.Column("output_data", sa.JSON, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        _create_index_safe(
            "ix_workflow_step_results_exec",
            "workflow_step_results",
            ["execution_id"],
        )
        _create_index_safe(
            "ix_workflow_step_results_status",
            "workflow_step_results",
            ["status"],
        )

    # =======================================================================
    # 4. trigger_configs.workflow_id — optional Workflow target
    # =======================================================================
    if _has_table("trigger_configs") and not _has_column("trigger_configs", "workflow_id"):
        # SQLite batch mode requires named FK constraints when rebuilding the table.
        with op.batch_alter_table("trigger_configs") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "workflow_id",
                    UUID(as_uuid=True),
                    nullable=True,
                )
            )
            batch_op.create_foreign_key(
                "fk_trigger_configs_workflow",
                "workflows",
                ["workflow_id"],
                ["id"],
                ondelete="SET NULL",
            )
        # Note: enforce XOR with action_type_id at the API layer (both nullable
        # to remain backward-compatible with HIA-75 tests).


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("trigger_configs") and _has_column("trigger_configs", "workflow_id"):
        with op.batch_alter_table("trigger_configs") as batch_op:
            batch_op.drop_constraint("fk_trigger_configs_workflow", type_="foreignkey")
            batch_op.drop_column("workflow_id")

    if _has_table("workflow_step_results"):
        for idx in (
            "ix_workflow_step_results_status",
            "ix_workflow_step_results_exec",
        ):
            if _has_index("workflow_step_results", idx):
                op.drop_index(idx, table_name="workflow_step_results")
        op.drop_table("workflow_step_results")

    if _has_table("workflow_executions"):
        for idx in (
            "ix_workflow_executions_status",
            "ix_workflow_executions_workflow",
            "ix_workflow_executions_project",
        ):
            if _has_index("workflow_executions", idx):
                op.drop_index(idx, table_name="workflow_executions")
        op.drop_table("workflow_executions")

    if _has_table("workflows"):
        if _has_index("workflows", "ix_workflows_project"):
            op.drop_index("ix_workflows_project", table_name="workflows")
        op.drop_table("workflows")

    _drop_enum_type("workflowstepstatus")
    _drop_enum_type("workflowexecutionstatus")
    _drop_enum_type("workflowstatus")

"""Function tables — HIA-70 C1.

Adds two tables to support the Action / Function editor:

* ``functions``     — named, reusable, versioned code units (python / javascript / typescript)
* ``function_runs`` — synchronous test-run records (editor "Run" button)

Revision ID: 0013_function
Revises: 0012_backup
Create Date: 2026-09-19 13:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0013_function"
down_revision: str | None = "0012_backup"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# helpers (kept identical to 0011_sso.py / 0012_backup.py for consistency)
# ---------------------------------------------------------------------------


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return any(idx["name"] == index_name for idx in insp.get_indexes(table))


def _has_unique(table: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return any(u["name"] == constraint_name for u in insp.get_unique_constraints(table))


def _create_index_safe(name: str, table: str, columns: list[str], **kw) -> None:
    if _has_index(table, name):
        return
    op.create_index(name, table, columns, **kw)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # =======================================================================
    # 1. functions
    # =======================================================================
    if not _has_table("functions"):
        op.create_table(
            "functions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("api_name", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "language",
                sa.String(20),
                nullable=False,
                server_default="python",
            ),
            sa.Column(
                "source_code",
                sa.Text,
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "version",
                sa.Integer,
                nullable=False,
                server_default="1",
            ),
            sa.Column("parameters_schema", sa.JSON, nullable=True),
            sa.Column("return_schema", sa.JSON, nullable=True),
            sa.Column("config", sa.JSON, nullable=True),
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            # TimestampMixin
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        _create_index_safe("ix_functions_project_id", "functions", ["project_id"])
        _create_index_safe("ix_functions_api_name", "functions", ["api_name"])
        # unique (project_id, api_name) — SQLite 不支持 ALTER ADD CONSTRAINT，
        # 直接用 unique index 实现；其他方言也兼容。
        if not _has_index("functions", "uq_functions_project_api_name"):
            op.create_index(
                "uq_functions_project_api_name",
                "functions",
                ["project_id", "api_name"],
                unique=True,
            )

    # =======================================================================
    # 2. function_runs
    # =======================================================================
    if not _has_table("function_runs"):
        op.create_table(
            "function_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "function_id",
                UUID(as_uuid=True),
                sa.ForeignKey("functions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("input_data", sa.JSON, nullable=True),
            sa.Column("output_data", sa.JSON, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("triggered_by", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        _create_index_safe(
            "ix_function_runs_project_id", "function_runs", ["project_id"]
        )
        _create_index_safe(
            "ix_function_runs_function_id", "function_runs", ["function_id"]
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("function_runs"):
        op.drop_table("function_runs")
    if _has_table("functions"):
        op.drop_table("functions")

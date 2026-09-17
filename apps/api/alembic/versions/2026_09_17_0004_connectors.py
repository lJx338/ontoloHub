"""connectors table (HIA-71 B6)

Adds the ``connectors`` table — one row per data source connection instance
per project.  Stores connection type, name, free-form ``config`` JSON
(sensitive fields encrypted at the application layer), last-tested cache, and
soft-delete columns.

Revision ID: 0004_connectors
Revises: 0003_link_identity_key
Create Date: 2026-09-17 14:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0004_connectors"
down_revision: str | None = "0003_link_identity_key"
branch_labels: str | None = None
depends_on: str | None = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if _has_table("connectors"):
        return

    # connector_type values match Python enum (see src/db/connector.py)
    op.create_table(
        "connectors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.String(50),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("secret_fields", sa.JSON, nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_message", sa.Text, nullable=True),
        sa.Column("last_test_ok", sa.Boolean, nullable=True),
        sa.Column(
            "default_sample_limit",
            sa.Integer,
            nullable=False,
            server_default="1000",
        ),
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
        # SoftDeleteMixin (project mixin columns)
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_connectors_project", "connectors", ["project_id"])
    op.create_index(
        "ix_connectors_project_type", "connectors", ["project_id", "type"]
    )


def downgrade() -> None:
    if not _has_table("connectors"):
        return
    op.drop_index("ix_connectors_project_type", table_name="connectors")
    op.drop_index("ix_connectors_project", table_name="connectors")
    op.drop_table("connectors")

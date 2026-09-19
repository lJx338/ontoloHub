"""Workspace / multi-tenant tables (HIA-77 D1).

Adds:

* ``workspaces`` — SaaS tenant boundary (one organization = one workspace).
* ``workspace_memberships`` — many-to-many User↔Workspace with a workspace
  role (``owner`` / ``admin`` / ``member`` / ``viewer``).
* ``projects.workspace_id`` — nullable FK so legacy single-tenant projects
  keep working.  When set, the API layer enforces per-workspace isolation.

This is the **MVP** shape — separate DB schemas per workspace and
``{workspace}.ontolohub.com`` URL routing are intentionally deferred to
D1.x.  D1 ships the data + API layer; tenants share a single DB but every
query is filtered through ``require_workspace_role`` / ``X-Workspace-Id``.

Revision ID: 0010_workspace
Revises: 0009_release_line
Create Date: 2026-09-19 10:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0010_workspace"
down_revision: str | None = "0009_release_line"
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


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # =======================================================================
    # 1. workspaces
    # =======================================================================
    if not _has_table("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "plan",
                sa.String(50),
                nullable=False,
                server_default="free",
            ),
            sa.Column(
                "settings",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "owner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            # SoftDeleteMixin + TimestampMixin
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
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", UUID(as_uuid=True), nullable=True),
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
        )
        _create_index_safe("ix_workspaces_slug", "workspaces", ["slug"])
        _create_index_safe("ix_workspaces_owner", "workspaces", ["owner_id"])
        _create_index_safe("ix_workspaces_plan", "workspaces", ["plan"])

    # =======================================================================
    # 2. workspace_memberships
    # =======================================================================
    if not _has_table("workspace_memberships"):
        op.create_table(
            "workspace_memberships",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "workspace_id",
                UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role",
                sa.String(50),
                nullable=False,
                server_default="member",
            ),
            sa.Column("invited_by", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
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
            sa.UniqueConstraint(
                "user_id",
                "workspace_id",
                name="uq_workspace_memberships_user_workspace",
            ),
        )
        _create_index_safe(
            "ix_workspace_memberships_user",
            "workspace_memberships",
            ["user_id"],
        )
        _create_index_safe(
            "ix_workspace_memberships_workspace",
            "workspace_memberships",
            ["workspace_id"],
        )

    # =======================================================================
    # 3. projects.workspace_id (nullable; legacy projects keep working)
    # =======================================================================
    if _has_table("projects") and not _has_column("projects", "workspace_id"):
        with op.batch_alter_table("projects") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "workspace_id",
                    UUID(as_uuid=True),
                    nullable=True,
                )
            )
            batch_op.create_foreign_key(
                "fk_projects_workspace",
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="SET NULL",
            )
        _create_index_safe("ix_projects_workspace", "projects", ["workspace_id"])


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("projects") and _has_column("projects", "workspace_id"):
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_constraint("fk_projects_workspace", type_="foreignkey")
            batch_op.drop_column("workspace_id")

    if _has_table("workspace_memberships"):
        for idx in (
            "ix_workspace_memberships_workspace",
            "ix_workspace_memberships_user",
        ):
            if _has_index("workspace_memberships", idx):
                op.drop_index(idx, table_name="workspace_memberships")
        op.drop_table("workspace_memberships")

    if _has_table("workspaces"):
        for idx in (
            "ix_workspaces_plan",
            "ix_workspaces_owner",
            "ix_workspaces_slug",
        ):
            if _has_index("workspaces", idx):
                op.drop_index(idx, table_name="workspaces")
        op.drop_table("workspaces")

"""identity schema additions (HIA-51)

0001 baseline already lays down the complete schema (including ``users``,
``memberships``, ``audit_events``) via ``Base.metadata.create_all``. This
revision only adds what the baseline does *not* yet cover:

- The composite index ``ix_audit_events_project_created`` on ``audit_events``
  to speed up project-scoped hash-chain walks.

The table creations are kept as ``checkfirst=True`` for safety in case
``users``/``memberships`` are absent (e.g. on databases bootstrapped from
the legacy SQL-only schema).

Revision ID: 0002_identity
Revises: 0001_baseline
Create Date: 2026-09-17 09:40:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0002_identity"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("email", sa.String(255), nullable=False, unique=True),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column(
                "global_role",
                sa.String(50),
                nullable=False,
                server_default="user",
            ),
            sa.Column(
                "is_active", sa.Boolean, nullable=False, server_default=sa.text("1")
            ),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
        op.create_index("ix_users_email", "users", ["email"])

    if not _has_table("memberships"):
        op.create_table(
            "memberships",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role",
                sa.String(50),
                nullable=False,
                server_default="viewer",
            ),
            sa.Column("invited_by", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "is_active", sa.Boolean, nullable=False, server_default=sa.text("1")
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
                "user_id", "project_id", name="uq_memberships_user_project"
            ),
        )
        op.create_index("ix_memberships_user", "memberships", ["user_id"])
        op.create_index("ix_memberships_project", "memberships", ["project_id"])

    # audit_events 已经由 0001 创建；这里追加组合索引以加速项目级追溯。
    op.create_index(
        "ix_audit_events_project_created",
        "audit_events",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_project_created", table_name="audit_events")
    if _has_table("memberships"):
        op.drop_index("ix_memberships_project", table_name="memberships")
        op.drop_index("ix_memberships_user", table_name="memberships")
        op.drop_table("memberships")
    if _has_table("users"):
        op.drop_index("ix_users_email", table_name="users")
        op.drop_table("users")

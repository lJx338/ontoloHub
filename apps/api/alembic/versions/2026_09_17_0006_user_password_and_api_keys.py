"""User 密码 + API Key 表（HIA-64 B1 — 多用户 / JWT / API Key）。

本次迁移：
1. ``users`` 表加 ``password_hash`` 字段（NULLable，向后兼容 bootstrap admin）
2. 新建 ``api_keys`` 表 — API Key 凭据（机器对机器）

Revision ID: 0006_user_password_and_api_keys
Revises: 0005_cr_workflow_enhancements
Create Date: 2026-09-17 18:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0006_user_password_and_api_keys"
down_revision: str | None = "0005_cr_workflow_enhancements"
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
    return any(c["name"] == column for c in insp.get_columns(table))


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
    # 1. users.password_hash — bcrypt 哈希（NULL 表示尚未设密码）
    # =======================================================================
    if _has_table("users") and not _has_column("users", "password_hash"):
        op.add_column(
            "users",
            sa.Column("password_hash", sa.String(255), nullable=True),
        )

    # =======================================================================
    # 2. api_keys 表
    # =======================================================================
    if not _has_table("api_keys"):
        op.create_table(
            "api_keys",
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
                nullable=True,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("key_prefix", sa.String(16), nullable=False),
            sa.Column(
                "scopes",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        )
        _create_index_safe(
            "ix_api_keys_key_hash",
            "api_keys",
            ["key_hash"],
        )
        _create_index_safe(
            "ix_api_keys_user_id",
            "api_keys",
            ["user_id"],
        )
        _create_index_safe(
            "ix_api_keys_project_id",
            "api_keys",
            ["project_id"],
        )
        _create_index_safe(
            "ix_api_keys_user_project",
            "api_keys",
            ["user_id", "project_id"],
        )
        _create_index_safe(
            "ix_api_keys_key_prefix",
            "api_keys",
            ["key_prefix"],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("api_keys"):
        for idx in (
            "ix_api_keys_key_prefix",
            "ix_api_keys_user_project",
            "ix_api_keys_project_id",
            "ix_api_keys_user_id",
            "ix_api_keys_key_hash",
        ):
            if _has_index("api_keys", idx):
                op.drop_index(idx, table_name="api_keys")
        op.drop_table("api_keys")

    if _has_table("users") and _has_column("users", "password_hash"):
        op.drop_column("users", "password_hash")

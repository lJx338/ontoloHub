"""SSO / Identity Provider tables (HIA-79 D2).

Adds:

* ``identity_providers`` — per-workspace IdP config (OIDC / SAML / LDAP).
  One provider per workspace per protocol is enforced by a unique
  constraint.

* ``sso_login_sessions`` — in-flight OIDC flow tracking (state / nonce /
  PKCE verifier / redirect_uri).  Short-lived (10 min default).

Revision ID: 0011_sso
Revises: 0010_workspace
Create Date: 2026-09-19 11:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0011_sso"
down_revision: str | None = "0010_workspace"
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


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # =======================================================================
    # 1. identity_providers
    # =======================================================================
    if not _has_table("identity_providers"):
        op.create_table(
            "identity_providers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "workspace_id",
                UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("protocol", sa.String(20), nullable=False),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="active",
            ),
            sa.Column(
                "config",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "claim_mapping",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "secret_fields",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "auto_provision",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "force_sso",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("last_test_status", sa.String(20), nullable=True),
            sa.Column("last_test_message", sa.Text, nullable=True),
            sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
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
            sa.UniqueConstraint(
                "workspace_id",
                "protocol",
                name="uq_identity_providers_workspace_protocol",
            ),
        )
        _create_index_safe(
            "ix_identity_providers_workspace",
            "identity_providers",
            ["workspace_id"],
        )
        _create_index_safe(
            "ix_identity_providers_protocol",
            "identity_providers",
            ["protocol"],
        )

    # =======================================================================
    # 2. sso_login_sessions
    # =======================================================================
    if not _has_table("sso_login_sessions"):
        op.create_table(
            "sso_login_sessions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "provider_id",
                UUID(as_uuid=True),
                sa.ForeignKey("identity_providers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "workspace_id",
                UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("state", sa.String(128), nullable=False),
            sa.Column("nonce", sa.String(128), nullable=False),
            sa.Column("code_verifier", sa.String(128), nullable=False),
            sa.Column("redirect_uri", sa.String(2048), nullable=False),
            sa.Column("relay_state", sa.String(2048), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("state", name="uq_sso_login_sessions_state"),
        )
        _create_index_safe(
            "ix_sso_login_sessions_provider",
            "sso_login_sessions",
            ["provider_id"],
        )
        _create_index_safe(
            "ix_sso_login_sessions_workspace",
            "sso_login_sessions",
            ["workspace_id"],
        )
        _create_index_safe(
            "ix_sso_login_sessions_state",
            "sso_login_sessions",
            ["state"],
        )
        _create_index_safe(
            "ix_sso_login_sessions_expires",
            "sso_login_sessions",
            ["expires_at"],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("sso_login_sessions"):
        for idx in (
            "ix_sso_login_sessions_expires",
            "ix_sso_login_sessions_state",
            "ix_sso_login_sessions_workspace",
            "ix_sso_login_sessions_provider",
        ):
            if _has_index("sso_login_sessions", idx):
                op.drop_index(idx, table_name="sso_login_sessions")
        op.drop_table("sso_login_sessions")

    if _has_table("identity_providers"):
        for idx in (
            "ix_identity_providers_protocol",
            "ix_identity_providers_workspace",
        ):
            if _has_index("identity_providers", idx):
                op.drop_index(idx, table_name="identity_providers")
        op.drop_table("identity_providers")

"""links.identity_key column (HIA-59)

Revision ID: 0003_link_identity_key
Revises: 0002_identity
Create Date: 2026-09-17 13:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0003_link_identity_key"
down_revision: str | None = "0002_identity"
branch_labels: str | None = None
depends_on: str | None = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


def _has_column(table: str, col: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_table("links"):
        return  # baseline may not have created it yet; SQLAlchemy create_all in app will

    if not _has_column("links", "identity_key"):
        op.add_column(
            "links",
            sa.Column("identity_key", sa.String(500), nullable=True),
        )
        op.create_index(
            "ix_links_identity_key", "links", ["identity_key"], unique=False
        )


def downgrade() -> None:
    if _has_table("links") and _has_column("links", "identity_key"):
        op.drop_index("ix_links_identity_key", table_name="links")
        op.drop_column("links", "identity_key")

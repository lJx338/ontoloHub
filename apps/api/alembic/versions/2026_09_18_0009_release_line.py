"""Release line tables — ontology branches / tags / merge audit (HIA-74 D4).

Adds:

* ``ontology_branches`` — named branch of an Ontology inside a project
  (default ``main``; ``is_default=True`` for the canonical line;
   ``is_protected=True`` blocks force-push / delete).
* ``ontology_tags``    — immutable marker pointing to a specific
  OntologyVersion (Git-tag style; we don't enforce immutability at the
  schema level but the API never exposes ``PATCH version_id``).
* ``branch_merges``    — audit record of every branch merge operation
  (fast_forward / merge_commit / noop). Append-only.

These complement (not replace) ``ontology_versions``, which remains the
source of truth for ontology content snapshots.  Branches/tags add the
*lineage* layer.

Note: the ``(project_id, ontology_id, is_default=True)`` invariant is
enforced at the API layer (see ``ensure_default_branch``); we don't use
a partial unique index here to keep the migration portable across
SQLite and PostgreSQL.

Revision ID: 0009_release_line
Revises: 0008_workflow
Create Date: 2026-09-18 14:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0009_release_line"
down_revision: str | None = "0008_workflow"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# helpers (kept identical to 0008_workflow.py for consistency)
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
    bind.execute(
        sa.text(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})")
    )


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
    # Enum types (Postgres only — SQLite stores strings)
    # =======================================================================
    _create_enum_type(
        "mergestrategy",
        ["fast_forward", "merge_commit", "noop"],
    )

    # =======================================================================
    # 1. ontology_branches
    # =======================================================================
    if not _has_table("ontology_branches"):
        op.create_table(
            "ontology_branches",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "ontology_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontologies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "is_default",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "is_protected",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "head_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_versions.id", ondelete="SET NULL"),
                nullable=True,
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
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            sa.UniqueConstraint(
                "project_id",
                "ontology_id",
                "name",
                name="uq_ontology_branches_proj_ontology_name",
            ),
        )
        _create_index_safe(
            "ix_ontology_branches_proj_ontology",
            "ontology_branches",
            ["project_id", "ontology_id"],
        )
        _create_index_safe(
            "ix_ontology_branches_project",
            "ontology_branches",
            ["project_id"],
        )

    # =======================================================================
    # 2. ontology_tags
    # =======================================================================
    if not _has_table("ontology_tags"):
        op.create_table(
            "ontology_tags",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "ontology_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontologies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_versions.id", ondelete="CASCADE"),
                nullable=False,
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
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            sa.UniqueConstraint(
                "project_id",
                "ontology_id",
                "name",
                name="uq_ontology_tags_proj_ontology_name",
            ),
        )
        _create_index_safe(
            "ix_ontology_tags_proj_ontology",
            "ontology_tags",
            ["project_id", "ontology_id"],
        )
        _create_index_safe(
            "ix_ontology_tags_project",
            "ontology_tags",
            ["project_id"],
        )

    # =======================================================================
    # 3. branch_merges (audit log)
    # =======================================================================
    if not _has_table("branch_merges"):
        op.create_table(
            "branch_merges",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_branch_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_branches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_branch_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_branches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_head_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "target_head_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "merge_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "strategy",
                sa.String(50),
                nullable=False,
                server_default="fast_forward",
            ),
            sa.Column("message", sa.Text, nullable=True),
            sa.Column("performed_by", UUID(as_uuid=True), nullable=True),
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
        )
        _create_index_safe(
            "ix_branch_merges_target_created",
            "branch_merges",
            ["target_branch_id", "created_at"],
        )
        _create_index_safe(
            "ix_branch_merges_project",
            "branch_merges",
            ["project_id"],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("branch_merges"):
        for idx in (
            "ix_branch_merges_project",
            "ix_branch_merges_target_created",
        ):
            if _has_index("branch_merges", idx):
                op.drop_index(idx, table_name="branch_merges")
        op.drop_table("branch_merges")

    if _has_table("ontology_tags"):
        for idx in (
            "ix_ontology_tags_project",
            "ix_ontology_tags_proj_ontology",
        ):
            if _has_index("ontology_tags", idx):
                op.drop_index(idx, table_name="ontology_tags")
        op.drop_table("ontology_tags")

    if _has_table("ontology_branches"):
        for idx in (
            "ix_ontology_branches_project",
            "ix_ontology_branches_proj_ontology",
        ):
            if _has_index("ontology_branches", idx):
                op.drop_index(idx, table_name="ontology_branches")
        op.drop_table("ontology_branches")

    _drop_enum_type("mergestrategy")

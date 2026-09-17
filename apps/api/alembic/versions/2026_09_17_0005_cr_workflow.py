"""CR workflow enhancements + release/deployment column backfills (HIA-69 B5)

This migration does several things in one revision because they all share the
same theme (HIA-69 CR workflow state machine + the model fields the API already
references but the DB schema never had):

1. ``change_requests`` — add columns for the new state machine and payload:

   * ``baseline_version_id`` / ``baseline_version``
   * ``target_version_id`` / ``target_version``
   * ``changes`` (JSON, default ``{}``)
   * ``changes_summary`` (TEXT)
   * ``impact_scope`` (JSON)
   * ``required_approvers`` (INT, default 1)
   * ``submitted_by`` / ``submitted_at``
   * ``reviewed_by``  (the model already had ``reviewer_id``; we keep both)
   * ``approved_by`` / ``approved_at``
   * ``merged_by``
   * ``closed_at`` / ``closed_by`` / ``close_reason``
   * ``created_by``

2. Extend the ``change_request_status`` PG enum with ``SUBMITTED`` /
   ``CHANGES_REQUESTED`` / ``CLOSED`` so the application enum's new values
   can round-trip the DB.  SQLite stores enums as strings + a CHECK
   constraint — the constraint is rebuilt by recreating the table for the
   new columns that reference enum-typed fields (none here), so nothing
   additional is needed for SQLite.

3. Create ``change_request_reviewers`` (M2M: which reviewers are assigned,
   each with its own status).

4. Create ``change_request_comments`` (threaded comments with ``parent_id``).

5. Backfill missing payload columns on the related tables so the API can
   read / write the shapes it already returns:

   * ``releases``  — ``ontology_version`` / ``mapping_version`` /
     ``artifacts`` / ``checksum`` / ``artifact_size`` /
     ``validation_results`` / ``released_at`` / ``released_by``
   * ``use_case_bundles`` — ``project_id`` / ``name`` / ``description`` /
     ``version`` / ``use_case_ids`` / ``ontology_version_id`` /
     ``mapping_version_id`` / ``validation_results`` / ``dependencies`` /
     ``manifest`` / ``artifact_path`` / ``checksum`` / ``created_by``
   * ``deployments`` — ``project_id`` / ``environment`` /
     ``environment_type`` / ``configuration`` / ``result`` /
     ``error_message`` / ``deployed_by_name``
   * ``preflight_reports`` — ``project_id`` / ``environment`` /
     ``release_version`` / ``blocking_issues`` / ``warnings`` /
     ``warnings_count`` / ``created_by``

Revision ID: 0005_cr_workflow_enhancements
Revises: 0004_connectors
Create Date: 2026-09-17 16:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0005_cr_workflow_enhancements"
down_revision: str | None = "0004_connectors"
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
    """op.create_index wrapper that is idempotent on SQLite (no IF NOT EXISTS)."""
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
    # 1. change_requests — new columns
    # =======================================================================
    if _has_table("change_requests"):
        new_cols: list[tuple[str, sa.Column]] = [
            ("baseline_version_id", sa.Column("baseline_version_id", UUID(as_uuid=True), nullable=True)),
            ("baseline_version", sa.Column("baseline_version", sa.String(50), nullable=True)),
            ("target_version_id", sa.Column("target_version_id", UUID(as_uuid=True), nullable=True)),
            ("target_version", sa.Column("target_version", sa.String(50), nullable=True)),
            ("changes", sa.Column("changes", sa.JSON, nullable=False, server_default=sa.text("'{}'"))),
            ("changes_summary", sa.Column("changes_summary", sa.Text, nullable=True)),
            ("impact_scope", sa.Column("impact_scope", sa.JSON, nullable=True)),
            ("required_approvers", sa.Column("required_approvers", sa.Integer, nullable=False, server_default=sa.text("1"))),
            ("submitted_by", sa.Column("submitted_by", UUID(as_uuid=True), nullable=True)),
            ("submitted_at", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True)),
            ("reviewed_by", sa.Column("reviewed_by", UUID(as_uuid=True), nullable=True)),
            ("approved_by", sa.Column("approved_by", UUID(as_uuid=True), nullable=True)),
            ("approved_at", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)),
            ("merged_by", sa.Column("merged_by", UUID(as_uuid=True), nullable=True)),
            ("closed_at", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)),
            ("closed_by", sa.Column("closed_by", UUID(as_uuid=True), nullable=True)),
            ("close_reason", sa.Column("close_reason", sa.Text, nullable=True)),
            ("created_by", sa.Column("created_by", UUID(as_uuid=True), nullable=True)),
        ]
        for col_name, col in new_cols:
            if not _has_column("change_requests", col_name):
                op.add_column("change_requests", col)

        _create_index_safe(
            "ix_change_requests_submitted_at",
            "change_requests",
            ["submitted_at"],
        )

    # =======================================================================
    # 2. Extend change_request_status enum (PG only)
    # =======================================================================
    if _is_postgres():
        bind = op.get_bind()
        # Add new values to the PG enum.  IF NOT EXISTS requires PG 9.6+.
        with bind.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for new_value in ("submitted", "changes_requested", "closed"):
                conn.execute(
                    sa.text(
                        f"ALTER TYPE change_request_status "
                        f"ADD VALUE IF NOT EXISTS '{new_value}'"
                    )
                )

    # =======================================================================
    # 3. change_request_reviewers (M2M)
    # =======================================================================
    if not _has_table("change_request_reviewers"):
        # reviewer_status enum
        if _is_postgres():
            op.execute(
                "DO $$ BEGIN "
                "CREATE TYPE reviewer_status AS ENUM "
                "('pending', 'approved', 'changes_requested'); "
                "EXCEPTION WHEN duplicate_object THEN null; END $$;"
            )
        reviewer_status = sa.Enum(
            "pending", "approved", "changes_requested",
            name="reviewer_status",
        )
        op.create_table(
            "change_request_reviewers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "change_request_id",
                UUID(as_uuid=True),
                sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("reviewer_id", UUID(as_uuid=True), nullable=False),
            sa.Column("reviewer_name", sa.String(255), nullable=True),
            sa.Column("status", reviewer_status, nullable=False, server_default="pending"),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("comment", sa.Text, nullable=True),
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
                "change_request_id", "reviewer_id",
                name="uq_cr_reviewer_cr_reviewer",
            ),
        )
        op.create_index(
            "ix_change_request_reviewers_change_request_id",
            "change_request_reviewers",
            ["change_request_id"],
        )
        op.create_index(
            "ix_cr_reviewers_status",
            "change_request_reviewers",
            ["status"],
        )

    # =======================================================================
    # 4. change_request_comments (threaded)
    # =======================================================================
    if not _has_table("change_request_comments"):
        op.create_table(
            "change_request_comments",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "change_request_id",
                UUID(as_uuid=True),
                sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "parent_id",
                UUID(as_uuid=True),
                sa.ForeignKey("change_request_comments.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("author_id", UUID(as_uuid=True), nullable=True),
            sa.Column("author_name", sa.String(255), nullable=True),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        op.create_index(
            "ix_change_request_comments_change_request_id",
            "change_request_comments",
            ["change_request_id"],
        )
        op.create_index(
            "ix_change_request_comments_parent_id",
            "change_request_comments",
            ["parent_id"],
        )
        op.create_index(
            "ix_cr_comments_cr_created",
            "change_request_comments",
            ["change_request_id", "created_at"],
        )

    # =======================================================================
    # 5. releases — backfill missing columns
    # =======================================================================
    if _has_table("releases"):
        for col_name, col in [
            ("ontology_version", sa.Column("ontology_version", sa.String(50), nullable=True)),
            ("mapping_version", sa.Column("mapping_version", sa.String(50), nullable=True)),
            ("artifacts", sa.Column("artifacts", sa.JSON, nullable=True)),
            ("checksum", sa.Column("checksum", sa.String(64), nullable=True)),
            ("artifact_size", sa.Column("artifact_size", sa.Integer, nullable=True)),
            ("validation_results", sa.Column("validation_results", sa.JSON, nullable=True)),
            ("released_at", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True)),
            ("released_by", sa.Column("released_by", UUID(as_uuid=True), nullable=True)),
        ]:
            if not _has_column("releases", col_name):
                op.add_column("releases", col)

    # =======================================================================
    # 6. use_case_bundles — backfill missing columns
    # =======================================================================
    if _has_table("use_case_bundles"):
        for col_name, col in [
            ("project_id", sa.Column("project_id", UUID(as_uuid=True), nullable=True)),
            ("name", sa.Column("name", sa.String(255), nullable=True)),
            ("description", sa.Column("description", sa.Text, nullable=True)),
            ("version", sa.Column("version", sa.String(50), nullable=True)),
            ("use_case_ids", sa.Column("use_case_ids", sa.JSON, nullable=True)),
            ("ontology_version_id", sa.Column("ontology_version_id", UUID(as_uuid=True), nullable=True)),
            ("mapping_version_id", sa.Column("mapping_version_id", UUID(as_uuid=True), nullable=True)),
            ("validation_results", sa.Column("validation_results", sa.JSON, nullable=True)),
            ("dependencies", sa.Column("dependencies", sa.JSON, nullable=True)),
            ("manifest", sa.Column("manifest", sa.JSON, nullable=True)),
            ("artifact_path", sa.Column("artifact_path", sa.String(500), nullable=True)),
            ("checksum", sa.Column("checksum", sa.String(64), nullable=True)),
            ("created_by", sa.Column("created_by", UUID(as_uuid=True), nullable=True)),
        ]:
            if not _has_column("use_case_bundles", col_name):
                op.add_column("use_case_bundles", col)
        _create_index_safe(
            "ix_use_case_bundles_project_id",
            "use_case_bundles",
            ["project_id"],
        )

    # =======================================================================
    # 7. deployments — backfill missing columns
    # =======================================================================
    if _has_table("deployments"):
        for col_name, col in [
            ("project_id", sa.Column("project_id", UUID(as_uuid=True), nullable=True)),
            ("environment", sa.Column("environment", sa.String(100), nullable=True)),
            ("environment_type", sa.Column("environment_type", sa.String(50), nullable=True)),
            ("configuration", sa.Column("configuration", sa.JSON, nullable=True)),
            ("result", sa.Column("result", sa.JSON, nullable=True)),
            ("error_message", sa.Column("error_message", sa.Text, nullable=True)),
            ("deployed_by_name", sa.Column("deployed_by_name", sa.String(255), nullable=True)),
        ]:
            if not _has_column("deployments", col_name):
                op.add_column("deployments", col)
        _create_index_safe(
            "ix_deployments_project_id",
            "deployments",
            ["project_id"],
        )

    # =======================================================================
    # 8. preflight_reports — backfill missing columns
    # =======================================================================
    if _has_table("preflight_reports"):
        for col_name, col in [
            ("project_id", sa.Column("project_id", UUID(as_uuid=True), nullable=True)),
            ("environment", sa.Column("environment", sa.String(100), nullable=True)),
            ("release_version", sa.Column("release_version", sa.String(50), nullable=True)),
            ("blocking_issues", sa.Column("blocking_issues", sa.JSON, nullable=True)),
            ("warnings", sa.Column("warnings", sa.JSON, nullable=True)),
            ("warnings_count", sa.Column("warnings_count", sa.Integer, nullable=False, server_default=sa.text("0"))),
            ("created_by", sa.Column("created_by", UUID(as_uuid=True), nullable=True)),
        ]:
            if not _has_column("preflight_reports", col_name):
                op.add_column("preflight_reports", col)
        _create_index_safe(
            "ix_preflight_project",
            "preflight_reports",
            ["project_id"],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # preflight_reports
    if _has_table("preflight_reports"):
        op.drop_index("ix_preflight_project", table_name="preflight_reports")
        for col in (
            "created_by", "warnings_count", "warnings", "blocking_issues",
            "release_version", "environment", "project_id",
        ):
            if _has_column("preflight_reports", col):
                op.drop_column("preflight_reports", col)

    # deployments
    if _has_table("deployments"):
        op.drop_index("ix_deployments_project_id", table_name="deployments")
        for col in (
            "deployed_by_name", "error_message", "result", "configuration",
            "environment_type", "environment", "project_id",
        ):
            if _has_column("deployments", col):
                op.drop_column("deployments", col)

    # use_case_bundles
    if _has_table("use_case_bundles"):
        op.drop_index("ix_use_case_bundles_project_id", table_name="use_case_bundles")
        for col in (
            "created_by", "checksum", "artifact_path", "manifest",
            "dependencies", "validation_results", "mapping_version_id",
            "ontology_version_id", "use_case_ids", "version", "description",
            "name", "project_id",
        ):
            if _has_column("use_case_bundles", col):
                op.drop_column("use_case_bundles", col)

    # releases
    if _has_table("releases"):
        for col in (
            "released_by", "released_at", "validation_results", "artifact_size",
            "checksum", "artifacts", "mapping_version", "ontology_version",
        ):
            if _has_column("releases", col):
                op.drop_column("releases", col)

    # comments
    if _has_table("change_request_comments"):
        op.drop_index("ix_cr_comments_cr_created", table_name="change_request_comments")
        op.drop_index("ix_change_request_comments_parent_id", table_name="change_request_comments")
        op.drop_index("ix_change_request_comments_change_request_id", table_name="change_request_comments")
        op.drop_table("change_request_comments")

    # reviewers
    if _has_table("change_request_reviewers"):
        op.drop_index("ix_cr_reviewers_status", table_name="change_request_reviewers")
        op.drop_index("ix_change_request_reviewers_change_request_id", table_name="change_request_reviewers")
        op.drop_table("change_request_reviewers")
        if _is_postgres():
            op.execute("DROP TYPE IF EXISTS reviewer_status")

    # change_requests columns
    if _has_table("change_requests"):
        op.drop_index("ix_change_requests_submitted_at", table_name="change_requests")
        for col in (
            "created_by", "close_reason", "closed_by", "closed_at", "merged_by",
            "approved_at", "approved_by", "reviewed_by", "submitted_at",
            "submitted_by", "required_approvers", "impact_scope",
            "changes_summary", "changes", "target_version", "target_version_id",
            "baseline_version", "baseline_version_id",
        ):
            if _has_column("change_requests", col):
                op.drop_column("change_requests", col)

    # PG enum value removal is intentionally NOT reversed — PG does not allow
    # DROP VALUE inside a transaction block in 12-.  Leaving the extra values
    # in place is harmless because the application no longer writes them.

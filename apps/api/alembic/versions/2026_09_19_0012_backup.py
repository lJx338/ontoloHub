"""Backup & DR tables — HIA-90 D5.

Adds four tables to support the end-to-end backup / restore / verify / DR-drill
flow described in the Linear issue:

* ``backups``          — every produced backup artefact
* ``restore_logs``     — every restore attempt (manual or drill)
* ``backup_schedules`` — cron-style schedule entries
* ``dr_drills``        — record of a full DR drill (RTO / RPO / smoke results)

Revision ID: 0012_backup
Revises: 0011_sso
Create Date: 2026-09-19 12:50:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

revision: str = "0012_backup"
down_revision: str | None = "0011_sso"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# helpers (kept identical to 0011_sso.py for consistency)
# ---------------------------------------------------------------------------


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return name in insp.get_table_names()


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
    # 1. backups
    # =======================================================================
    if not _has_table("backups"):
        op.create_table(
            "backups",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("storage_path", sa.String(1000), nullable=False),
            sa.Column(
                "storage_backend",
                sa.String(50),
                nullable=False,
                server_default="local",
            ),
            sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
            sa.Column("checksum", sa.String(64), nullable=True),
            sa.Column("is_encrypted", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("retention_days", sa.Integer, nullable=False, server_default="30"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("triggered_by", sa.String(50), nullable=False, server_default="manual"),
            sa.Column("triggered_by_user", UUID(as_uuid=True), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column(
                "parent_backup_id",
                UUID(as_uuid=True),
                sa.ForeignKey("backups.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("metadata", sa.JSON, nullable=True),
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
        _create_index_safe("ix_backups_kind", "backups", ["kind"])
        _create_index_safe("ix_backups_status", "backups", ["status"])
        _create_index_safe("ix_backups_status_created", "backups", ["status", "created_at"])
        _create_index_safe("ix_backups_kind_status", "backups", ["kind", "status"])
        _create_index_safe("ix_backups_expires", "backups", ["expires_at"])

    # =======================================================================
    # 2. restore_logs
    # =======================================================================
    if not _has_table("restore_logs"):
        op.create_table(
            "restore_logs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "backup_id",
                UUID(as_uuid=True),
                sa.ForeignKey("backups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("components", sa.JSON, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("restored_metadata", sa.JSON, nullable=True),
            sa.Column("performed_by", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "performed_by_label",
                sa.String(100),
                nullable=False,
                server_default="manual",
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
        )
        _create_index_safe("ix_restore_logs_backup_id", "restore_logs", ["backup_id"])
        _create_index_safe(
            "ix_restore_logs_backup_status", "restore_logs", ["backup_id", "status"],
        )

    # =======================================================================
    # 3. backup_schedules
    # =======================================================================
    if not _has_table("backup_schedules"):
        op.create_table(
            "backup_schedules",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("cron_expression", sa.String(100), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("retention_days", sa.Integer, nullable=False, server_default="30"),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "last_backup_id",
                UUID(as_uuid=True),
                sa.ForeignKey("backups.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("notes", sa.Text, nullable=True),
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
            "ix_backup_schedules_enabled_next", "backup_schedules", ["enabled", "next_run_at"],
        )

    # =======================================================================
    # 4. dr_drills
    # =======================================================================
    if not _has_table("dr_drills"):
        op.create_table(
            "dr_drills",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "backup_id",
                UUID(as_uuid=True),
                sa.ForeignKey("backups.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "restore_log_id",
                UUID(as_uuid=True),
                sa.ForeignKey("restore_logs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("rto_seconds", sa.Float, nullable=True),
            sa.Column("rpo_seconds", sa.Float, nullable=True),
            sa.Column("checklist", sa.JSON, nullable=True),
            sa.Column("report", sa.JSON, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("triggered_by", sa.String(50), nullable=False, server_default="manual"),
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
        _create_index_safe("ix_dr_drills_backup_id", "dr_drills", ["backup_id"])
        _create_index_safe("ix_dr_drills_status_started", "dr_drills", ["status", "started_at"])


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("dr_drills"):
        for idx in (
            "ix_dr_drills_status_started",
            "ix_dr_drills_backup_id",
        ):
            if _has_index("dr_drills", idx):
                op.drop_index(idx, table_name="dr_drills")
        op.drop_table("dr_drills")

    if _has_table("backup_schedules"):
        if _has_index("backup_schedules", "ix_backup_schedules_enabled_next"):
            op.drop_index(
                "ix_backup_schedules_enabled_next", table_name="backup_schedules",
            )
        op.drop_table("backup_schedules")

    if _has_table("restore_logs"):
        for idx in (
            "ix_restore_logs_backup_status",
            "ix_restore_logs_backup_id",
        ):
            if _has_index("restore_logs", idx):
                op.drop_index(idx, table_name="restore_logs")
        op.drop_table("restore_logs")

    if _has_table("backups"):
        for idx in (
            "ix_backups_expires",
            "ix_backups_kind_status",
            "ix_backups_status_created",
            "ix_backups_status",
            "ix_backups_kind",
        ):
            if _has_index("backups", idx):
                op.drop_index(idx, table_name="backups")
        op.drop_table("backups")

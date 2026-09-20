"""Webhook & Trigger tables (HIA-75 C3 — Webhook/Trigger integration)

Adds three tables for the webhook / trigger framework:

1. ``webhook_configs`` — outbound webhook subscriptions per project
2. ``webhook_deliveries`` — delivery attempt records (for retry + history)
3. ``trigger_configs`` — inbound webhook / cron schedule / object-change
   triggers that fire ActionType when their condition matches

Revision ID: 0007_webhook_trigger
Revises: 0006_user_password_and_api_keys
Create Date: 2026-09-18 14:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid as UUID, inspect as sqla_inspect

# revision identifiers, used by Alembic.
revision: str = "0007_webhook_trigger"
down_revision: str | None = "0006_user_password_and_api_keys"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# helpers
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


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_enum_type(name: str, values: list[str]) -> None:
    """Create a Postgres enum type (no-op on SQLite)."""
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})"))


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
    # Enum types (Postgres only; SQLite stores as VARCHAR with no constraint)
    # =======================================================================
    _create_enum_type(
        "webhookdeliverystatus",
        ["pending", "success", "failed", "retrying", "dropped"],
    )
    _create_enum_type(
        "triggertype",
        ["inbound_webhook", "schedule", "object_change"],
    )
    _create_enum_type(
        "triggerstatus",
        ["active", "paused", "error"],
    )

    # =======================================================================
    # 1. webhook_configs — outbound webhook subscriptions
    # =======================================================================
    if not _has_table("webhook_configs"):
        op.create_table(
            "webhook_configs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("url", sa.String(2048), nullable=False),
            # List of event names this webhook subscribes to (e.g. ["cr.merged"])
            sa.Column("events", sa.JSON, nullable=False, server_default="[]"),
            # Optional JSONata / JSONPath filter
            sa.Column("filter_expression", sa.Text, nullable=True),
            # HMAC-SHA256 secret used to sign payloads
            sa.Column("secret", sa.String(255), nullable=False),
            # Retry config
            sa.Column("retry_count", sa.Integer, nullable=False, server_default="3"),
            sa.Column(
                "retry_delay_seconds",
                sa.Integer,
                nullable=False,
                server_default="60",
            ),
            sa.Column(
                "is_enabled",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("1"),
            ),
            # Statistics
            sa.Column(
                "total_deliveries",
                sa.Integer,
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "failed_deliveries",
                sa.Integer,
                nullable=False,
                server_default="0",
            ),
            sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text, nullable=True),
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
            # ProjectMixin
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", UUID(as_uuid=True), nullable=True),
            sa.Column("created_by", UUID(as_uuid=True), nullable=True),
            sa.UniqueConstraint(
                "project_id", "name", name="uq_webhook_config_project_name"
            ),
        )
        _create_index_safe("ix_webhook_configs_project", "webhook_configs", ["project_id"])

    # =======================================================================
    # 2. webhook_deliveries — delivery attempt log
    # =======================================================================
    if not _has_table("webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "webhook_config_id",
                UUID(as_uuid=True),
                sa.ForeignKey("webhook_configs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(100), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("http_status_code", sa.Integer, nullable=True),
            sa.Column("response_body", sa.Text, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
            sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("target_type", sa.String(100), nullable=True),
            sa.Column("target_id", sa.String(255), nullable=True),
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
            "ix_webhook_deliveries_config",
            "webhook_deliveries",
            ["webhook_config_id"],
        )
        _create_index_safe(
            "ix_webhook_deliveries_config_status",
            "webhook_deliveries",
            ["webhook_config_id", "status"],
        )
        _create_index_safe(
            "ix_webhook_deliveries_target",
            "webhook_deliveries",
            ["target_type", "target_id"],
        )

    # =======================================================================
    # 3. trigger_configs — inbound webhook / schedule / object-change
    # =======================================================================
    if not _has_table("trigger_configs"):
        op.create_table(
            "trigger_configs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "trigger_type",
                sa.String(50),
                nullable=False,
            ),
            # JSON config: {token: "..."} for inbound_webhook
            #             {cron: "*/5 * * * *"} for schedule
            #             {filter: "..."} for object_change
            sa.Column("trigger_config", sa.JSON, nullable=True),
            sa.Column(
                "action_type_id",
                UUID(as_uuid=True),
                sa.ForeignKey("action_types.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("input_template", sa.JSON, nullable=True),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="active",
            ),
            sa.Column("total_runs", sa.Integer, nullable=False, server_default="0"),
            sa.Column("failed_runs", sa.Integer, nullable=False, server_default="0"),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text, nullable=True),
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
        )
        _create_index_safe("ix_trigger_configs_project", "trigger_configs", ["project_id"])
        _create_index_safe(
            "ix_trigger_configs_type_status",
            "trigger_configs",
            ["trigger_type", "status"],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if _has_table("trigger_configs"):
        for idx in ("ix_trigger_configs_type_status", "ix_trigger_configs_project"):
            if _has_index("trigger_configs", idx):
                op.drop_index(idx, table_name="trigger_configs")
        op.drop_table("trigger_configs")

    if _has_table("webhook_deliveries"):
        for idx in (
            "ix_webhook_deliveries_target",
            "ix_webhook_deliveries_config_status",
            "ix_webhook_deliveries_config",
        ):
            if _has_index("webhook_deliveries", idx):
                op.drop_index(idx, table_name="webhook_deliveries")
        op.drop_table("webhook_deliveries")

    if _has_table("webhook_configs"):
        if _has_index("webhook_configs", "ix_webhook_configs_project"):
            op.drop_index("ix_webhook_configs_project", table_name="webhook_configs")
        op.drop_table("webhook_configs")

    _drop_enum_type("triggerstatus")
    _drop_enum_type("triggertype")
    _drop_enum_type("webhookdeliverystatus")

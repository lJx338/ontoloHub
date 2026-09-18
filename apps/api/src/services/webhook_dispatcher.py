"""Webhook dispatcher service — HIA-75 C3.

Emits outbound webhook events with HMAC-SHA256 signatures and automatic retry.
Also handles inbound webhook processing and trigger execution.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import text as _text, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import async_session_factory
from src.db.webhook import (
    WebhookConfig,
    WebhookDelivery,
    WebhookEventType,
    WebhookDeliveryStatus,
    TriggerConfig,
    TriggerType,
    TriggerStatus,
)
from src.db.runtime import ActionType, ActionRun, ActionRunStatus

logger = logging.getLogger(__name__)


# =============================================================================
# Signature helpers
# =============================================================================


def _compute_signature(secret: str, payload: bytes) -> str:
    """Compute HMAC-SHA256 signature of payload using the webhook secret."""
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _build_headers(payload: dict, secret: str) -> dict:
    """Build request headers including HMAC signature."""
    body_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    signature = _compute_signature(secret, body_bytes)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signed_payload = f"{timestamp}.{body_bytes.decode('utf-8')}"
    return {
        "Content-Type": "application/json",
        "X-OntoloHub-Signature": f"sha256={signature}",
        "X-OntoloHub-Timestamp": timestamp,
        "User-Agent": "OntoloHub-Webhook/1.0",
    }


# =============================================================================
# Event payload builder
# =============================================================================


def build_event_payload(
    event_type: WebhookEventType | str,
    target_type: str,
    target_id: uuid.UUID,
    actor_id: Optional[uuid.UUID] = None,
    actor_name: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Build a standard webhook event payload."""
    return {
        "event": event_type.value if isinstance(event_type, WebhookEventType) else event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": {
            "id": str(actor_id) if actor_id else None,
            "name": actor_name,
        },
        "target": {
            "type": target_type,
            "id": str(target_id),
        },
        **(extra or {}),
    }


# =============================================================================
# Core dispatcher
# =============================================================================


async def dispatch_webhook(
    event_type: WebhookEventType | str,
    project_id: uuid.UUID | str,
    payload: dict,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
) -> list[uuid.UUID]:
    """Find all enabled webhooks for the project listening to ``event_type``,
    create delivery records, and fire them off asynchronously.

    Returns list of created delivery IDs.
    """
    # Accept both UUID and string for ergonomics; SQLAlchemy UUID(as_uuid=True)
    # processor calls .hex on the value, so we need to convert here.
    if isinstance(project_id, str):
        project_id = uuid.UUID(project_id)

    event_name = event_type.value if isinstance(event_type, WebhookEventType) else event_type

    async with async_session_factory() as session:
        result = await session.execute(
            select(WebhookConfig).where(
                WebhookConfig.project_id == project_id,
                WebhookConfig.is_enabled == True,
            )
        )
        configs = result.scalars().all()

    delivery_ids: list[uuid.UUID] = []
    tasks = []

    for cfg in configs:
        # Check if this webhook subscribes to this event type
        if event_name not in (cfg.events or []):
            continue

        # Create delivery record
        delivery_id = await _create_delivery_record(
            cfg_id=cfg.id,
            event_type=event_name,
            payload=payload,
            target_type=target_type,
            target_id=target_id,
        )
        delivery_ids.append(delivery_id)

        # Fire off async delivery
        tasks.append(
            asyncio.create_task(
                _deliver_with_retry(
                    delivery_id=delivery_id,
                    url=cfg.url,
                    payload=payload,
                    secret=cfg.secret,
                    retry_count=cfg.retry_count,
                    retry_delay=cfg.retry_delay_seconds,
                )
            )
        )

    # Run all deliveries concurrently (fire-and-forget)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    return delivery_ids


async def _create_delivery_record(
    cfg_id: uuid.UUID,
    event_type: str,
    payload: dict,
    target_type: Optional[str],
    target_id: Optional[uuid.UUID],
) -> uuid.UUID:
    """Create a WebhookDelivery record in a fresh session."""
    async with async_session_factory() as session:
        delivery = WebhookDelivery(
            webhook_config_id=cfg_id,
            event_type=event_type,
            payload=payload,
            status=WebhookDeliveryStatus.PENDING,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
        )
        session.add(delivery)
        await session.flush()
        await session.refresh(delivery)
        delivery_id = delivery.id
    return delivery_id


async def _deliver_with_retry(
    delivery_id: uuid.UUID,
    url: str,
    payload: dict,
    secret: str,
    retry_count: int,
    retry_delay: int,
) -> None:
    """Send webhook payload with exponential-backoff retry."""
    headers = _build_headers(payload, secret)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    for attempt in range(1, retry_count + 1):
        status_code, response_body, duration_ms, error = await _http_post(url, body, headers, timeout=30.0)

        async with async_session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
            )
            delivery = result.scalar_one_or_none()
            if not delivery:
                return

            delivery.http_status_code = status_code
            delivery.response_body = response_body[:2000] if response_body else None
            delivery.duration_ms = duration_ms
            delivery.attempt = attempt

            if 200 <= (status_code or 0) < 300:
                delivery.status = WebhookDeliveryStatus.SUCCESS
                delivery.delivered_at = datetime.now(timezone.utc)
                delivery.webhook_config.total_deliveries += 1
                delivery.webhook_config.last_delivered_at = datetime.now(timezone.utc)
                delivery.webhook_config.last_error = None
                await session.flush()
                logger.info("Webhook delivered successfully: delivery_id=%s", delivery_id)
                return

            # Failure
            delivery.error_message = error
            delivery.webhook_config.failed_deliveries += 1
            delivery.webhook_config.last_error = error

            if attempt < retry_count:
                delivery.status = WebhookDeliveryStatus.RETRYING
                await session.flush()
                logger.warning(
                    "Webhook delivery failed (attempt %d/%d), retrying in %ds: delivery_id=%s error=%s",
                    attempt, retry_count, retry_delay, delivery_id, error,
                )
                await asyncio.sleep(retry_delay * (2 ** (attempt - 1)))  # exponential backoff
            else:
                delivery.status = WebhookDeliveryStatus.DROPPED
                delivery.delivered_at = datetime.now(timezone.utc)
                await session.flush()
                logger.error(
                    "Webhook delivery dropped after %d attempts: delivery_id=%s error=%s",
                    retry_count, delivery_id, error,
                )


async def _http_post(
    url: str,
    body: str,
    headers: dict,
    timeout: float,
) -> tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    """Perform HTTP POST and return (status_code, response_body, duration_ms, error)."""
    started = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(url, content=body, headers=headers)
            duration_ms = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            )
            return resp.status_code, resp.text, duration_ms, None
    except httpx.TimeoutException:
        duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        return None, None, duration_ms, "Request timeout"
    except httpx.RequestError as exc:
        duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        return None, None, duration_ms, f"Request error: {exc}"


# =============================================================================
# Inbound webhook handler
# =============================================================================


async def process_inbound_webhook(
    token: str,
    payload: dict,
    headers: dict,
) -> tuple[bool, Optional[str], Optional[uuid.UUID]]:
    """Process an inbound webhook request.

    Finds the TriggerConfig with matching INBOUND_WEBHOOK token,
    creates an ActionRun, and dispatches it.

    Returns (success, error_message, trigger_config_id).
    """
    from src.db.connection import sync_session_factory
    matched_trigger_id = None
    matched_action_type_id = None
    matched_project_id = None
    matched_template = None

    # Use sync session + raw SQL to avoid async/sync transaction visibility race
    # when this handler is called immediately after the trigger was just
    # created via the HTTP endpoint (see HIA-75 test isolation notes).
    with sync_session_factory() as session:
        result = session.execute(
            _text(
                "SELECT id, action_type_id, project_id, input_template "
                "FROM trigger_configs "
                "WHERE trigger_type = 'INBOUND_WEBHOOK' "
                "  AND status = 'ACTIVE' "
                "  AND json_extract(trigger_config, '$.token') = :token"
            ),
            {"token": token},
        )
        row = result.first()
        if row:
            matched_trigger_id = row[0]
            matched_action_type_id = row[1]
            matched_project_id = row[2]
            try:
                matched_template = json.loads(row[3]) if row[3] else None
            except Exception:
                matched_template = None

    if not matched_trigger_id:
        return False, "Token not found or trigger not active", None

    if isinstance(matched_trigger_id, str):
        matched_trigger_id = uuid.UUID(matched_trigger_id)
    if isinstance(matched_action_type_id, str):
        matched_action_type_id = uuid.UUID(matched_action_type_id)
    if isinstance(matched_project_id, str):
        matched_project_id = uuid.UUID(matched_project_id)

    # Load the ActionType (sync)
    # SQLite stores UUID as 32-char hex (no dashes); convert for raw SQL.
    from src.db.runtime import ActionType as _AT
    at_id_hex = matched_action_type_id.hex
    with sync_session_factory() as session:
        at_row = session.execute(
            _text("SELECT id, project_id, kind, code, runtime, config FROM action_types WHERE id = :id"),
            {"id": at_id_hex},
        ).first()
        if not at_row:
            return False, f"ActionType {matched_action_type_id} not found", matched_trigger_id
        action_type = _AT(
            id=uuid.UUID(str(at_row[0])),
            project_id=uuid.UUID(str(at_row[1])),
            kind=at_row[2],
            code=at_row[3],
            runtime=at_row[4],
            config=json.loads(at_row[5]) if at_row[5] else None,
        )

    # Build input_data from template + payload
    input_data = _apply_template(matched_template, {
        "webhook": {
            "headers": dict(headers),
            "payload": payload,
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    })

    # Create ActionRun + update trigger stats (sync)
    # SQLite stores UUID as 32-char hex (no dashes); convert for raw SQL.
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with sync_session_factory() as session:
        session.execute(
            _text(
                "INSERT INTO action_runs "
                "(id, project_id, action_type_id, status, input_data, "
                " triggered_by, automation_rule_id, created_at, updated_at) "
                "VALUES (:id, :project_id, :action_type_id, :status, "
                "        :input_data, :triggered_by, :automation_rule_id, :now, :now)"
            ),
            {
                "id": run_id.hex,
                "project_id": matched_project_id.hex,
                "action_type_id": matched_action_type_id.hex,
                "status": "PENDING",
                "input_data": json.dumps(input_data, ensure_ascii=False),
                "triggered_by": "webhook",
                "automation_rule_id": matched_trigger_id.hex,
                "now": now.isoformat(),
            },
        )
        session.execute(
            _text(
                "UPDATE trigger_configs SET total_runs = total_runs + 1, last_run_at = :now "
                "WHERE id = :id"
            ),
            {"id": matched_trigger_id.hex, "now": now.isoformat()},
        )
        session.commit()

    # Dispatch execution via async (sandbox executor is async)
    asyncio.create_task(_execute_action_run(run_id, action_type))

    return True, None, matched_trigger_id


async def _execute_action_run(run_id: uuid.UUID, action_type: ActionType) -> None:
    """Execute an ActionRun asynchronously (triggered by inbound webhook or schedule)."""
    started = datetime.now(timezone.utc)

    async def _mark(status: ActionRunStatus, output: Optional[dict] = None, error: Optional[str] = None) -> None:
        async with async_session_factory() as s:
            result = await s.execute(select(ActionRun).where(ActionRun.id == run_id))
            run = result.scalar_one_or_none()
            if not run:
                return
            run.status = status
            run.started_at = started
            if output is not None:
                run.output_data = output
            if error:
                run.error = error
            completed = datetime.now(timezone.utc)
            run.completed_at = completed
            run.duration_ms = int((completed - started).total_seconds() * 1000)

            # Update trigger stats
            trigger_result = await s.execute(
                select(TriggerConfig).where(TriggerConfig.id == run.automation_rule_id)
            )
            trigger = trigger_result.scalar_one_or_none()
            if trigger:
                if status == ActionRunStatus.SUCCESS:
                    trigger.last_error = None
                else:
                    trigger.failed_runs += 1
                    trigger.last_error = error
                    trigger.status = TriggerStatus.ERROR

            await s.flush()

    await _mark(ActionRunStatus.RUNNING)

    try:
        if action_type.kind == "webhook":
            url = (action_type.config or {}).get("url", "")
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(url)
            await _mark(ActionRunStatus.SUCCESS, output={"status_code": resp.status_code, "body": resp.text[:500]})
        elif action_type.kind == "function":
            # Delegate to sandbox executor
            from src.api.action import _sandbox_execute
            async with async_session_factory() as s:
                result = await s.execute(select(ActionRun).where(ActionRun.id == run_id))
                run = result.scalar_one_or_none()
                input_data = run.input_data if run else {}

            code = action_type.code or ""
            runtime = (action_type.runtime or "python").lower()
            result = await _sandbox_execute(code, runtime, input_data or {}, action_type.config)
            await _mark(ActionRunStatus.SUCCESS, output=result)
        else:
            await _mark(ActionRunStatus.FAILED, error=f"Unsupported kind: {action_type.kind}")
    except Exception as exc:
        logger.exception("Trigger action execution failed: run_id=%s", run_id)
        await _mark(ActionRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


def _apply_template(template: Optional[dict], context: dict) -> dict:
    """Simple JSON template substitution: replace {{key.path}} in string values."""
    if not template:
        return context

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            # Simple {{var.path}} substitution
            import re
            def replacer(m: re.Match) -> str:
                path = m.group(1).strip().split(".")
                v = context
                for key in path:
                    if isinstance(v, dict):
                        v = v.get(key, m.group(0))
                    else:
                        return m.group(0)
                return str(v) if v is not None else m.group(0)
            return re.sub(r"\{\{([^}]+)\}\}", replacer, value)
        elif isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    return _sub(template)


# =============================================================================
# Cron scheduler for schedule triggers
# =============================================================================


async def run_cron_triggers() -> None:
    """Called periodically (e.g. every minute) to fire due schedule triggers."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    # This function dispatches due cron triggers
    async with async_session_factory() as session:
        result = await session.execute(
            select(TriggerConfig).where(
                TriggerConfig.trigger_type == TriggerType.SCHEDULE,
                TriggerConfig.status == TriggerStatus.ACTIVE,
            )
        )
        triggers = result.scalars().all()

    for trigger in triggers:
        cfg = trigger.trigger_config or {}
        cron_expr = cfg.get("cron")
        if not cron_expr:
            continue

        # Evaluate cron expression against current time
        # Simple check: for "*/5 * * * *" fire every 5 minutes
        now = datetime.now(timezone.utc)
        if _cron_matches(cron_expr, now):
            await _fire_schedule_trigger(trigger)


def _cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Simple cron matching for common patterns.

    Supports:
    - */N (every N units)
    - exact value
    - comma-separated values
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minute, hour, day, month, dow = parts

        if not _cron_field_matches(minute, dt.minute, 0, 59):
            return False
        if not _cron_field_matches(hour, dt.hour, 0, 23):
            return False
        if not _cron_field_matches(day, dt.day, 1, 31):
            return False
        if not _cron_field_matches(month, dt.month, 1, 12):
            return False
        if not _cron_field_matches(dow, dt.isoweekday() % 7, 0, 6):
            # Sunday = 0 in cron
            return False
        return True
    except Exception:
        return False


def _cron_field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
    """Check if a cron field matches the given value."""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return (value - min_val) % step == 0
    if "," in field:
        return str(value) in [v.strip() for v in field.split(",")]
    return int(field) == value


async def _fire_schedule_trigger(trigger: TriggerConfig) -> None:
    """Fire a schedule trigger by creating an ActionRun."""
    async with async_session_factory() as session:
        at_result = await session.execute(
            select(ActionType).where(ActionType.id == trigger.action_type_id)
        )
        action_type = at_result.scalar_one_or_none()
        if not action_type:
            logger.warning("Schedule trigger %s: ActionType %s not found", trigger.id, trigger.action_type_id)
            return

        input_data = _apply_template(trigger.input_template, {
            "schedule": {
                "fired_at": datetime.now(timezone.utc).isoformat(),
                "cron": (trigger.trigger_config or {}).get("cron", ""),
            },
        })

        run = ActionRun(
            project_id=trigger.project_id,
            action_type_id=trigger.action_type_id,
            status=ActionRunStatus.PENDING,
            input_data=input_data,
            triggered_by="schedule",
            automation_rule_id=trigger.id,
        )
        session.add(run)
        trigger.total_runs += 1
        trigger.last_run_at = datetime.now(timezone.utc)
        await session.flush()
        await session.refresh(run)
        await session.commit()

        asyncio.create_task(_execute_action_run(run.id, action_type))
        logger.info("Schedule trigger fired: trigger_id=%s run_id=%s", trigger.id, run.id)


# =============================================================================
# Token generation
# =============================================================================


def generate_webhook_token() -> str:
    """Generate a random token for inbound webhook URLs."""
    return secrets.token_urlsafe(32)


def generate_webhook_secret() -> str:
    """Generate a random secret for HMAC signing."""
    return secrets.token_hex(32)

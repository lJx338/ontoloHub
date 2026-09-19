"""Workflow step handlers — HIA-76 C4.

Each handler is a pure-async function that:
  - receives ``step`` (dict from Workflow.steps), ``input_data`` (resolved),
    and an async ``AsyncSession``
  - returns a dict that becomes ``WorkflowStepResult.output_data``

Step types:
  - ``function_call`` : call an ActionType (kind=function) via sandbox
  - ``webhook_call``  : call a WebhookConfig's outbound HTTP
  - ``object_api``    : mutate Object / Link tables directly
  - ``delay``         : sleep N seconds

All handlers are designed to be called from the async executor's event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.runtime import ActionType, ActionRun, ActionRunStatus
from src.db.webhook import (
    WebhookConfig,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEventType,
)
from src.db.object_ import Object, Link, ObjectStatus, ObjectType

logger = logging.getLogger(__name__)


# ===========================================================================
# Step result type alias
# ===========================================================================

StepResult = dict
"""A handler returns a dict representing the step's output."""


# ===========================================================================
# Validation
# ===========================================================================


def validate_step(step: dict, project_id: Optional[uuid.UUID] = None) -> Optional[str]:
    """Return an error message string if ``step`` is invalid, else ``None``."""
    if not isinstance(step, dict):
        return "step must be an object"
    if not step.get("id"):
        return "step.id is required"
    if not step.get("type"):
        return "step.type is required"
    if step["type"] not in {"function_call", "webhook_call", "object_api", "delay"}:
        return f"unknown step.type: {step['type']}"
    # Per-type required fields
    if step["type"] in {"function_call", "webhook_call", "object_api"} and not step.get("ref"):
        return f"step.type={step['type']} requires ref"
    if step["type"] == "delay":
        cfg = step.get("config") or {}
        if not isinstance(cfg.get("seconds"), (int, float)) or cfg["seconds"] < 0:
            return "step.type=delay requires config.seconds >= 0"
    return None


# ===========================================================================
# function_call — ActionType(kind=function)
# ===========================================================================


async def run_function_step(
    step: dict,
    input_data: dict,
    session: AsyncSession,
) -> StepResult:
    """Execute a function ActionType synchronously and return its output.

    The handler delegates to the same sandbox used by ActionRun.
    """
    ref = step.get("ref")
    if not ref:
        raise ValueError("function_call step requires ref (ActionType id)")

    at_id = _parse_uuid(ref, "function_call.ref")
    result = await session.execute(select(ActionType).where(ActionType.id == at_id))
    at = result.scalar_one_or_none()
    if not at:
        raise LookupError(f"ActionType {at_id} not found")
    if at.kind != "function":
        raise ValueError(
            f"ActionType {at_id} kind={at.kind} is not callable as function"
        )

    code = at.code or ""
    runtime = (at.runtime or "python").lower()
    cfg = at.config or {}
    timeout_s = int(step.get("config", {}).get("timeout_s") or cfg.get("timeout_s", 30))

    # Lazy import — sandbox module is heavy.
    from src.runtime.sandbox import execute_python, execute_javascript, SandboxError

    try:
        if runtime == "python":
            sbx = execute_python(
                code=code,
                input_data=input_data,
                secrets=cfg.get("secrets"),
                timeout=timeout_s,
            )
        elif runtime == "javascript":
            sbx = execute_javascript(
                code=code,
                input_data=input_data,
                timeout=timeout_s,
            )
        else:
            raise ValueError(f"unknown runtime: {runtime}")

        # SandboxResult may have .to_dict() or be a plain dataclass-like
        out = sbx.to_dict() if hasattr(sbx, "to_dict") else sbx.__dict__
    except SandboxError as exc:
        # Bubble up so the executor records this as a step failure with
        # the sandbox's stderr. The error message becomes the step's error.
        raise RuntimeError(f"[sandbox:{exc.kind}] {exc.message}") from exc
    except Exception as exc:  # pragma: no cover — surface unexpected failures
        logger.exception("function step sandbox crashed")
        raise

    # Sandbox returns a SandboxResult envelope; surface runtime failures as
    # exceptions so the executor records the step as FAILED rather than
    # silently treating it as success.
    if isinstance(out, dict):
        if out.get("timed_out"):
            raise RuntimeError(
                f"sandbox timed out after {out.get('duration_ms', '?')}ms"
            )
        # User code raised an exception: the sandbox runner writes
        # ``{"_error": "<ExcType>: <message>"}`` into the result envelope so
        # the original exception type/message propagate to the workflow step
        # failure record (instead of "no <<<RESULT>>> marker found").
        result_payload = out.get("result")
        if isinstance(result_payload, dict) and "_error" in result_payload:
            raise RuntimeError(str(result_payload["_error"]))
        exit_code = out.get("exit_code", 0)
        if exit_code not in (0, None):
            stderr = (out.get("stderr") or "").strip().splitlines()[-1] if out.get("stderr") else ""
            raise RuntimeError(
                f"sandbox exited with code {exit_code}: {stderr or '(no stderr)'}"
            )
        if out.get("result") is None and (out.get("stderr") or "").strip():
            # Non-zero exit + no result + stderr present: treat as failure even
            # if exit_code is missing.
            raise RuntimeError(
                f"sandbox failed: {(out.get('stderr') or '').strip().splitlines()[-1]}"
            )

    # Convention: extract .result if present, else the whole dict.
    if isinstance(out, dict) and "result" in out:
        return out
    return {"result": out}


# ===========================================================================
# webhook_call — WebhookConfig (outbound)
# ===========================================================================


async def run_webhook_step(
    step: dict,
    input_data: dict,
    session: AsyncSession,
) -> StepResult:
    """Fire a configured outbound WebhookConfig.

    Re-uses ``WebhookConfig.secret`` for HMAC-SHA256 signing (same scheme as
    webhook_dispatcher). We do NOT create a ``WebhookDelivery`` row here to
    keep the step transactional; if observability is needed later, add a
    ``delivery_log=True`` flag.
    """
    from src.services.webhook_dispatcher import _compute_signature

    ref = step.get("ref")
    if not ref:
        raise ValueError("webhook_call step requires ref (WebhookConfig id)")

    cfg_id = _parse_uuid(ref, "webhook_call.ref")
    result = await session.execute(select(WebhookConfig).where(WebhookConfig.id == cfg_id))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise LookupError(f"WebhookConfig {cfg_id} not found")
    if not cfg.is_enabled:
        raise RuntimeError(f"WebhookConfig {cfg_id} is disabled")

    # Build payload: input_data + event metadata
    payload = dict(input_data or {})
    payload.setdefault("event", "workflow.step.webhook_call")
    payload.setdefault("fired_at", datetime.now(timezone.utc).isoformat())

    body_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    signature = _compute_signature(cfg.secret, body_bytes)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-OntoloHub-Signature": f"sha256={signature}",
        "X-OntoloHub-Timestamp": timestamp,
        "User-Agent": "OntoloHub-Workflow/1.0",
    }

    cfg_step = step.get("config") or {}
    timeout_s = float(cfg_step.get("timeout_s", 30))

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        resp = await client.post(cfg.url, content=body_bytes, headers=headers)

    return {
        "status_code": resp.status_code,
        "body": resp.text[:2000],
        "ok": 200 <= resp.status_code < 300,
        "url": cfg.url,
    }


# ===========================================================================
# object_api — internal Object / Link mutation
# ===========================================================================


async def run_object_api_step(
    step: dict,
    input_data: dict,
    session: AsyncSession,
    project_id: uuid.UUID,
) -> StepResult:
    """Operate on Object/Link tables directly.

    ``step.config.operation`` is one of:
      - ``create_object``  → create Object; returns id
      - ``update_object``  → update by id from input_data or config.target_id
      - ``create_link``    → create Link between two objects
    """
    cfg = step.get("config") or {}
    operation = cfg.get("operation")
    if not operation:
        raise ValueError("object_api step requires config.operation")

    if operation == "create_object":
        # Required: object_type_iri, name. data defaults to input_data.
        iri = cfg.get("object_type_iri") or step.get("ref")
        if not iri:
            raise ValueError("create_object requires object_type_iri (or step.ref)")
        name = (input_data.get("name") if isinstance(input_data, dict) else None) \
            or cfg.get("name") or input_data.get("email") if isinstance(input_data, dict) else None
        if not name:
            raise ValueError("create_object requires input_data.name or config.name")

        data = cfg.get("data") or input_data
        obj = Object(
            project_id=project_id,
            ontology_class_iri=iri,
            object_type=ObjectType(cfg.get("object_type", "entity")),
            name=str(name),
            description=cfg.get("description"),
            identity_key=(input_data.get("identity_key") if isinstance(input_data, dict) else None)
                         or cfg.get("identity_key"),
            data=data if isinstance(data, dict) else {"value": data},
            status=ObjectStatus.ACTIVE,
        )
        session.add(obj)
        await session.flush()
        await session.refresh(obj)
        return {"id": str(obj.id), "name": obj.name, "operation": "create_object"}

    if operation == "update_object":
        target_id = cfg.get("target_id")
        if not target_id and isinstance(input_data, dict):
            target_id = input_data.get("id")
        if not target_id:
            raise ValueError("update_object requires config.target_id or input_data.id")
        obj_id = _parse_uuid(target_id, "update_object.target_id")

        result = await session.execute(
            select(Object).where(Object.id == obj_id, Object.project_id == project_id)
        )
        obj = result.scalar_one_or_none()
        if not obj:
            raise LookupError(f"Object {obj_id} not found in project {project_id}")

        patch = cfg.get("data") or {}
        if isinstance(input_data, dict):
            # Only top-level keys from input_data that are not 'id' may patch.
            for k, v in input_data.items():
                if k in {"id", "project_id"}:
                    continue
                if k not in patch:
                    patch[k] = v
        if "name" in patch:
            obj.name = str(patch["name"])
        if "description" in patch:
            obj.description = patch["description"]
        if "data" in patch and isinstance(patch["data"], dict):
            obj.data = {**(obj.data or {}), **patch["data"]}
        if "status" in patch:
            obj.status = ObjectStatus(patch["status"])
        if "object_type" in patch:
            obj.object_type = ObjectType(patch["object_type"])

        await session.flush()
        await session.refresh(obj)
        return {"id": str(obj.id), "name": obj.name, "operation": "update_object"}

    if operation == "create_link":
        src = cfg.get("source_id") or (input_data.get("source_id") if isinstance(input_data, dict) else None)
        tgt = cfg.get("target_id") or (input_data.get("target_id") if isinstance(input_data, dict) else None)
        rel_iri = cfg.get("relation_iri") or cfg.get("relation")
        if not (src and tgt and rel_iri):
            raise ValueError("create_link requires source_id, target_id, relation_iri")

        link = Link(
            project_id=project_id,
            source_id=_parse_uuid(src, "source_id"),
            target_id=_parse_uuid(tgt, "target_id"),
            ontology_relation_iri=str(rel_iri),
            identity_key=(input_data.get("identity_key") if isinstance(input_data, dict) else None),
            data=cfg.get("data") or {},
            status="active",
        )
        session.add(link)
        await session.flush()
        await session.refresh(link)
        return {
            "id": str(link.id),
            "source_id": str(link.source_id),
            "target_id": str(link.target_id),
            "operation": "create_link",
        }

    raise ValueError(f"unknown object_api operation: {operation}")


# ===========================================================================
# delay
# ===========================================================================


async def run_delay_step(
    step: dict,
    input_data: dict,
    session: AsyncSession,
) -> StepResult:
    cfg = step.get("config") or {}
    seconds = float(cfg.get("seconds", 0))
    if seconds > 0:
        await asyncio.sleep(seconds)
    return {"slept_seconds": seconds, "echo": input_data}


# ===========================================================================
# Input mapping — JSONPath-ish resolver
# ===========================================================================


_PLACEHOLDER_RE = re.compile(r"^\$([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)*)$")


def resolve_input_mapping(mapping: Any, context: dict) -> Any:
    """Resolve ``input_mapping`` against ``context``.

    Supported:
      - ``None`` or missing → return ``context`` as-is
      - ``str`` like ``"$trigger.payload.email"`` → walk context
      - ``dict`` → recursively resolve each value
      - ``list`` → resolve each element
      - anything else → return unchanged
    """
    if mapping is None:
        return context
    if isinstance(mapping, str):
        m = _PLACEHOLDER_RE.match(mapping.strip())
        if not m:
            return mapping
        path = m.group(1).split(".")
        v: Any = context
        for key in path:
            if isinstance(v, dict) and key in v:
                v = v[key]
            else:
                return mapping  # unresolved → return original placeholder
        return v
    if isinstance(mapping, dict):
        return {k: resolve_input_mapping(v, context) for k, v in mapping.items()}
    if isinstance(mapping, list):
        return [resolve_input_mapping(v, context) for v in mapping]
    return mapping


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_uuid(value: Any, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a UUID, got {value!r}")

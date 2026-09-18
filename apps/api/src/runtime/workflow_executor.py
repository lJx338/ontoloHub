"""Workflow execution engine — HIA-76 C4.

``execute_workflow(execution_id, trigger_kind, trigger_id)`` is the entry point
called by both the HTTP /execute endpoint and the trigger dispatcher
(inbound webhook / schedule).

Design notes:
  * Sequential executor (no parallel/loop/conditional yet — those are in the
    backlog). Each step is awaited in declaration order.
  * Each step's ``output_data`` is exposed to subsequent steps via the
    context dict under ``$steps.<step_id>``. The previous step's full output
    is also exposed as ``$prev`` for convenience.
  * On step failure the executor checks ``step.error_handler``:
      - ``"stop"`` (default) → fail the entire execution
      - ``"continue"``        → record SKIPPED error, continue
      - ``{"goto_step": "id"}`` → jump to a later step (no loops)
  * ``step.retry_policy`` controls per-step retry with exponential backoff.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import async_session_factory
from src.db.workflow import (
    Workflow,
    WorkflowExecution,
    WorkflowExecutionStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from src.runtime.workflow_step_handlers import (
    resolve_input_mapping,
    run_delay_step,
    run_function_step,
    run_object_api_step,
    run_webhook_step,
    validate_step,
)
from typing import Any  # noqa: E402

logger = logging.getLogger(__name__)


def _unwrap_step_output(step_output: dict) -> Any:
    """Unwrap a sandbox-style step output for context passing.

    Convention: when a step returns ``{"result": ..., "stdout": ..., "stderr": ...}``
    we expose ``result`` to the next step's ``$prev`` reference (and ``$steps.<id>``)
    so that ``$prev.field`` accesses the user-returned dict directly.
    The full step_output is still stored in ``final_output[step_id]`` for audit.
    """
    if isinstance(step_output, dict) and "result" in step_output and len(step_output) <= 8:
        return step_output["result"]
    return step_output


async def execute_workflow(
    execution_id: uuid.UUID,
    trigger_kind: str = "manual",
    trigger_id: Optional[uuid.UUID] = None,
    *,
    session: Optional[AsyncSession] = None,
) -> WorkflowExecution:
    """Run a queued WorkflowExecution. Returns the final execution row.

    When called from a sync HTTP request that has just created the
    WorkflowExecution row, pass the original ``session=`` so all executor
    DB operations share one transaction (avoids SQLite's writer-lock conflict).

    Background (async) callers can omit ``session`` — the executor will
    open its own async session.
    """
    started = datetime.now(timezone.utc)
    owns_session = session is None
    if owns_session:
        session = async_session_factory()
        session_ctx = session
    else:
        session_ctx = None  # type: ignore[assignment]

    async def _maybe_acquire():
        if owns_session:
            await session.__aenter__()  # type: ignore[union-attr]
        return session

    async def _maybe_release():
        if owns_session:
            await session.__aexit__(None, None, None)  # type: ignore[union-attr]

    try:
        await _maybe_acquire()

        # Phase 1: load + transition PENDING → RUNNING
        result = await session.execute(  # type: ignore[union-attr]
            select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
        )
        execution = result.scalar_one_or_none()
        if not execution:
            logger.warning("execute_workflow: execution %s not found", execution_id)
            raise LookupError(f"WorkflowExecution {execution_id} not found")

        wf_result = await session.execute(  # type: ignore[union-attr]
            select(Workflow).where(Workflow.id == execution.workflow_id)
        )
        workflow = wf_result.scalar_one_or_none()
        if not workflow:
            execution.status = WorkflowExecutionStatus.FAILED
            execution.error = "workflow definition not found"
            execution.completed_at = datetime.now(timezone.utc)
            execution.duration_ms = int(
                (execution.completed_at - started).total_seconds() * 1000
            )
            await session.flush()  # type: ignore[union-attr]
            return execution

        if execution.status not in (
            WorkflowExecutionStatus.PENDING,
            WorkflowExecutionStatus.RUNNING,
        ):
            logger.info(
                "execute_workflow: execution %s status=%s, skipping",
                execution_id,
                execution.status,
            )
            return execution

        execution.status = WorkflowExecutionStatus.RUNNING
        execution.started_at = started
        execution.trigger_kind = trigger_kind
        execution.trigger_id = trigger_id
        await session.flush()  # type: ignore[union-attr]

        project_id = execution.project_id
        input_context = execution.input_context or {}
        steps_def = list(workflow.steps or [])

        # Phase 2: execute steps sequentially
        context: dict = {
            "trigger": {
                "kind": trigger_kind,
                "id": str(trigger_id) if trigger_id else None,
                "payload": input_context,
            },
            "input": input_context,
            "steps": {},
            "prev": None,
        }

        final_output: dict = {}
        exec_error: Optional[str] = None
        final_status = WorkflowExecutionStatus.SUCCESS
        step_index = 0
        goto_target: Optional[str] = None
        skipping_until_goto = False

        while step_index < len(steps_def):
            step = steps_def[step_index]
            step_id = step.get("id") or f"step-{step_index}"

            # Handle goto skip
            if skipping_until_goto:
                if goto_target and step_id == goto_target:
                    skipping_until_goto = False
                    goto_target = None
                else:
                    await _record_step_result(
                        session,  # type: ignore[arg-type]
                        execution_id=execution_id,
                        step=step,
                        step_index=step_index,
                        status=WorkflowStepStatus.SKIPPED,
                        error="skipped due to error_handler.goto_step",
                    )
                    step_index += 1
                    continue

            # Validate step (defensive — API should already have validated).
            err = validate_step(step)
            if err:
                await _record_step_result(
                    session,  # type: ignore[arg-type]
                    execution_id=execution_id,
                    step=step,
                    step_index=step_index,
                    status=WorkflowStepStatus.FAILED,
                    error=f"step validation failed: {err}",
                )
                exec_error = err
                final_status = WorkflowExecutionStatus.FAILED
                break

            # Resolve input
            mapping = step.get("input_mapping") or {}
            resolved_input = resolve_input_mapping(mapping, context)
            if not isinstance(resolved_input, dict):
                resolved_input = {"value": resolved_input}

            # Run step with retry
            retry_cfg = step.get("retry_policy") or {}
            max_attempts = max(1, int(retry_cfg.get("max_attempts", 1)))
            delay_s = float(retry_cfg.get("delay_s", 1))

            step_output: Optional[dict] = None
            step_err: Optional[str] = None
            attempt = 0
            step_started = datetime.now(timezone.utc)
            for attempt in range(1, max_attempts + 1):
                try:
                    step_output = await _dispatch_step(
                        step=step,
                        input_data=resolved_input,
                        project_id=project_id,
                        session=session,  # type: ignore[arg-type]
                    )
                    step_err = None
                    break
                except Exception as exc:
                    step_err = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "workflow step failed (attempt %d/%d): execution=%s step=%s err=%s",
                        attempt, max_attempts, execution_id, step_id, step_err,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(delay_s * (2 ** (attempt - 1)))

            step_completed = datetime.now(timezone.utc)
            duration_ms = int((step_completed - step_started).total_seconds() * 1000)

            if step_output is not None and step_err is None:
                await _record_step_result(
                    session,  # type: ignore[arg-type]
                    execution_id=execution_id,
                    step=step,
                    step_index=step_index,
                    status=WorkflowStepStatus.SUCCESS,
                    input_data=resolved_input,
                    output_data=step_output,
                    attempt=attempt,
                    started_at=step_started,
                    completed_at=step_completed,
                    duration_ms=duration_ms,
                )
                # Expose to next step via $prev / $steps.<id>:
                # unwrap sandbox envelope so $prev.field hits user-returned data.
                prev_payload = _unwrap_step_output(step_output)
                context["steps"][step_id] = prev_payload
                context["prev"] = prev_payload
                final_output[step_id] = step_output
                step_index += 1
            else:
                await _record_step_result(
                    session,  # type: ignore[arg-type]
                    execution_id=execution_id,
                    step=step,
                    step_index=step_index,
                    status=WorkflowStepStatus.FAILED,
                    input_data=resolved_input,
                    error=step_err,
                    attempt=attempt,
                    started_at=step_started,
                    completed_at=step_completed,
                    duration_ms=duration_ms,
                )
                handler = step.get("error_handler", "stop")
                if handler == "continue":
                    context["prev"] = {"error": step_err}
                    final_output[step_id] = {"error": step_err}
                    step_index += 1
                    continue
                if isinstance(handler, dict) and handler.get("goto_step"):
                    goto_target = handler["goto_step"]
                    skipping_until_goto = True
                    exec_error = step_err
                    final_status = WorkflowExecutionStatus.SUCCESS
                    step_index += 1
                    continue
                exec_error = step_err
                final_status = WorkflowExecutionStatus.FAILED
                break

        # Phase 3: finalize execution
        completed = datetime.now(timezone.utc)
        duration_ms = int((completed - started).total_seconds() * 1000)

        execution.status = final_status
        execution.output = final_output if final_output else None
        execution.error = exec_error
        execution.completed_at = completed
        execution.duration_ms = duration_ms

        # Workflow stats
        workflow.total_executions = (workflow.total_executions or 0) + 1
        if final_status == WorkflowExecutionStatus.FAILED:
            workflow.failed_executions = (workflow.failed_executions or 0) + 1
        workflow.last_executed_at = completed

        await session.flush()  # type: ignore[union-attr]
        # Owned-session path must persist (caller did not start a transaction
        # we can rely on); the shared-session path is committed by the caller.
        if owns_session:
            await session.commit()  # type: ignore[union-attr]
        return execution
    except Exception:
        if owns_session and session is not None:
            try:
                await session.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
        raise
    finally:
        await _maybe_release()


# ===========================================================================
# Internal dispatch + persistence helpers
# ===========================================================================


async def _dispatch_step(
    step: dict,
    input_data: dict,
    project_id: uuid.UUID,
    session: AsyncSession,
) -> dict:
    """Run a single step using the shared session.

    Each step reuses the caller's session so all writes happen in one
    transaction (avoids SQLite writer-lock conflicts when the executor
    is invoked from a sync HTTP handler).
    """
    step_type = step["type"]
    if step_type == "function_call":
        return await run_function_step(step, input_data, session)
    if step_type == "webhook_call":
        return await run_webhook_step(step, input_data, session)
    if step_type == "object_api":
        return await run_object_api_step(step, input_data, session, project_id)
    if step_type == "delay":
        return await run_delay_step(step, input_data, session)
    raise ValueError(f"unknown step type: {step_type}")


async def _record_step_result(
    session: AsyncSession,
    execution_id: uuid.UUID,
    step: dict,
    step_index: int,
    status: WorkflowStepStatus,
    input_data: Optional[dict] = None,
    output_data: Optional[dict] = None,
    error: Optional[str] = None,
    attempt: int = 1,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Persist a single WorkflowStepResult row using the shared session."""
    result = WorkflowStepResult(
        execution_id=execution_id,
        step_id=str(step.get("id") or f"step-{step_index}"),
        step_index=step_index,
        step_type=str(step["type"]),
        step_name=step.get("name"),
        status=status,
        input_data=input_data,
        output_data=output_data,
        error=error,
        attempt=attempt,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
    )
    session.add(result)
    await session.flush()


# (legacy no-op kept for backward compat; not used by executor)
async def _mark_step_result_running(*args, **kwargs) -> None:
    return None

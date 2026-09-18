"""Workflow API — HIA-76 C4 Action 编排 (Workflow 多步执行).

Endpoints:
  POST   /projects/{project_id}/workflows               — create
  GET    /projects/{project_id}/workflows               — list
  GET    /projects/{project_id}/workflows/{wid}         — detail
  PATCH  /projects/{project_id}/workflows/{wid}         — update (draft only)
  DELETE /projects/{project_id}/workflows/{wid}         — delete (draft only)
  POST   /projects/{project_id}/workflows/{wid}/execute — manual run
  GET    /workflow-executions/{eid}                     — execution detail
  GET    /projects/{project_id}/workflow-executions     — list executions
  GET    /workflow-executions/{eid}/step-results        — per-step breakdown
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.workflow import (
    Workflow,
    WorkflowExecution,
    WorkflowExecutionStatus,
    WorkflowStatus,
    WorkflowStepResult,
)
from src.api.auth import get_current_user, require_project_role, record_audit
from src.db.identity import Role
from src.db.governance import AuditEventType
from src.runtime.workflow_step_handlers import validate_step
from src.runtime.workflow_executor import execute_workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["Workflows"])
exec_router = APIRouter(tags=["Workflows"])


# ===========================================================================
# Helpers
# ===========================================================================


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_steps(steps: list[dict]) -> Optional[str]:
    """Validate every step + check id uniqueness."""
    seen: set[str] = set()
    for i, step in enumerate(steps):
        err = validate_step(step)
        if err:
            return f"steps[{i}]: {err}"
        sid = str(step.get("id"))
        if sid in seen:
            return f"steps[{i}]: duplicate id '{sid}'"
        seen.add(sid)
    return None


# ===========================================================================
# Pydantic models
# ===========================================================================


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    steps: list[dict] = Field(..., min_length=1, description="Ordered step list")
    default_input: Optional[dict] = None

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, v: list[dict]) -> list[dict]:
        err = _validate_steps(v)
        if err:
            raise ValueError(err)
        return v


class WorkflowUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(draft|active|archived)$")
    steps: Optional[list[dict]] = Field(None, min_length=1)
    default_input: Optional[dict] = None

    @field_validator("steps")
    @classmethod
    def _check_steps(cls, v: Optional[list[dict]]) -> Optional[list[dict]]:
        if v is None:
            return v
        err = _validate_steps(v)
        if err:
            raise ValueError(err)
        return v


class WorkflowResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    status: str
    steps: list
    default_input: Optional[dict]
    total_executions: int
    failed_executions: int
    last_executed_at: Optional[str]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class WorkflowExecuteRequest(BaseModel):
    input: Optional[dict] = Field(
        default_factory=dict,
        description="Override of default_input for this execution",
    )
    async_run: bool = Field(
        default=False,
        description="If true, return immediately and run in background",
    )


class WorkflowExecutionResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    workflow_id: uuid.UUID
    status: str
    trigger_kind: str
    trigger_id: Optional[uuid.UUID]
    input_context: dict
    output: Optional[dict]
    error: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    triggered_by: Optional[str]
    created_at: str
    updated_at: str


class WorkflowStepResultResponse(BaseModel):
    id: uuid.UUID
    execution_id: uuid.UUID
    step_id: str
    step_index: int
    step_type: str
    step_name: Optional[str]
    status: str
    input_data: Optional[dict]
    output_data: Optional[dict]
    error: Optional[str]
    attempt: int
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    created_at: str


# ===========================================================================
# Converters
# ===========================================================================


def _workflow_to_response(w: Workflow) -> WorkflowResponse:
    return WorkflowResponse(
        id=w.id,
        project_id=w.project_id,
        name=w.name,
        description=w.description,
        status=w.status.value if w.status else "draft",
        steps=w.steps or [],
        default_input=w.default_input,
        total_executions=w.total_executions or 0,
        failed_executions=w.failed_executions or 0,
        last_executed_at=_iso(w.last_executed_at),
        created_by=w.created_by,
        created_at=_iso(w.created_at),
        updated_at=_iso(w.updated_at),
    )


def _execution_to_response(e: WorkflowExecution) -> WorkflowExecutionResponse:
    return WorkflowExecutionResponse(
        id=e.id,
        project_id=e.project_id,
        workflow_id=e.workflow_id,
        status=e.status.value if e.status else "pending",
        trigger_kind=e.trigger_kind or "manual",
        trigger_id=e.trigger_id,
        input_context=e.input_context or {},
        output=e.output,
        error=e.error,
        started_at=_iso(e.started_at),
        completed_at=_iso(e.completed_at),
        duration_ms=e.duration_ms,
        triggered_by=e.triggered_by,
        created_at=_iso(e.created_at),
        updated_at=_iso(e.updated_at),
    )


def _step_result_to_response(r: WorkflowStepResult) -> WorkflowStepResultResponse:
    return WorkflowStepResultResponse(
        id=r.id,
        execution_id=r.execution_id,
        step_id=r.step_id,
        step_index=r.step_index,
        step_type=r.step_type,
        step_name=r.step_name,
        status=r.status.value if r.status else "pending",
        input_data=r.input_data,
        output_data=r.output_data,
        error=r.error,
        attempt=r.attempt or 1,
        started_at=_iso(r.started_at),
        completed_at=_iso(r.completed_at),
        duration_ms=r.duration_ms,
        created_at=_iso(r.created_at),
    )


# ===========================================================================
# Workflow CRUD
# ===========================================================================


@router.post(
    "/{project_id}/workflows",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    project_id: uuid.UUID,
    data: WorkflowCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkflowResponse:
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    w = Workflow(
        project_id=project_id,
        name=data.name,
        description=data.description,
        status=WorkflowStatus.DRAFT,
        steps=data.steps,
        default_input=data.default_input,
        created_by=user.user.id if hasattr(user, "user") else None,
    )
    session.add(w)
    await session.flush()
    await session.refresh(w)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=project_id,
        target_type="workflow",
        target_id=str(w.id),
        after={"name": w.name, "step_count": len(data.steps)},
    )
    return _workflow_to_response(w)


@router.get(
    "/{project_id}/workflows",
    response_model=list[WorkflowResponse],
)
async def list_workflows(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status"),
    include_archived: bool = Query(False),
) -> list[WorkflowResponse]:
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(Workflow).where(Workflow.project_id == project_id)
    if status_filter:
        query = query.where(Workflow.status == WorkflowStatus(status_filter))
    elif not include_archived:
        query = query.where(Workflow.status != WorkflowStatus.ARCHIVED)
    query = query.order_by(Workflow.created_at.desc())

    result = await session.execute(query)
    return [_workflow_to_response(w) for w in result.scalars().all()]


@router.get(
    "/{project_id}/workflows/{workflow_id}",
    response_model=WorkflowResponse,
)
async def get_workflow(
    project_id: uuid.UUID,
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkflowResponse:
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.project_id == project_id,
        )
    )
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Workflow 不存在")
    return _workflow_to_response(w)


@router.patch(
    "/{project_id}/workflows/{workflow_id}",
    response_model=WorkflowResponse,
)
async def update_workflow(
    project_id: uuid.UUID,
    workflow_id: uuid.UUID,
    data: WorkflowUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkflowResponse:
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.project_id == project_id,
        )
    )
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Workflow 不存在")

    # steps can only be edited while DRAFT
    if data.steps is not None and w.status != WorkflowStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail="只能在 DRAFT 状态下修改 steps",
        )

    if data.name is not None:
        w.name = data.name
    if data.description is not None:
        w.description = data.description
    if data.status is not None:
        w.status = WorkflowStatus(data.status)
    if data.steps is not None:
        w.steps = data.steps
    if data.default_input is not None:
        w.default_input = data.default_input

    await session.flush()
    await session.refresh(w)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=project_id,
        target_type="workflow",
        target_id=str(w.id),
        after={"name": w.name},
    )
    return _workflow_to_response(w)


@router.delete(
    "/{project_id}/workflows/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workflow(
    project_id: uuid.UUID,
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    await require_project_role(project_id, Role.OWNER, principal=user, session=session)
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.project_id == project_id,
        )
    )
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Workflow 不存在")
    if w.status != WorkflowStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail="只能删除 DRAFT 状态的 Workflow；已激活请归档",
        )

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=project_id,
        target_type="workflow",
        target_id=str(workflow_id),
        after={"name": w.name},
    )
    await session.delete(w)
    await session.flush()


# ===========================================================================
# Manual execution
# ===========================================================================


@router.post(
    "/{project_id}/workflows/{workflow_id}/execute",
    response_model=WorkflowExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def execute_workflow_endpoint(
    project_id: uuid.UUID,
    workflow_id: uuid.UUID,
    data: WorkflowExecuteRequest,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkflowExecutionResponse:
    """手动触发 Workflow。

    - ``async_run=false``（默认）：等待执行完成，返回最终状态。
    - ``async_run=true``：立即返回 PENDING 记录，executor 在后台跑。
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.project_id == project_id,
        )
    )
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Workflow 不存在")

    if w.status not in {WorkflowStatus.DRAFT, WorkflowStatus.ACTIVE}:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow 状态 {w.status.value} 不可执行",
        )

    # Merge default_input + override input
    merged_input: dict = {}
    if w.default_input:
        merged_input.update(w.default_input)
    if data.input:
        merged_input.update(data.input)

    execution = WorkflowExecution(
        project_id=project_id,
        workflow_id=workflow_id,
        status=WorkflowExecutionStatus.PENDING,
        trigger_kind="manual",
        input_context=merged_input,
        triggered_by=str(user.user.id if hasattr(user, "user") else user.id),
    )
    session.add(execution)
    await session.flush()
    await session.refresh(execution)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=project_id,
        target_type="workflow_execution",
        target_id=str(execution.id),
        after={"workflow_id": str(workflow_id), "async_run": data.async_run},
    )

    if data.async_run:
        # Background execution
        asyncio.create_task(
            _safe_execute(execution.id, trigger_kind="manual")
        )
        return _execution_to_response(execution)

    # Sync execution — share session so all executor writes happen in one
    # transaction (avoids SQLite writer-lock conflict).
    await session.flush()
    await execute_workflow(
        execution.id, trigger_kind="manual", session=session,
    )
    await session.commit()
    # Re-load to return fully-populated response.
    result = await session.execute(
        select(WorkflowExecution).where(WorkflowExecution.id == execution.id)
    )
    final = result.scalar_one()
    return _execution_to_response(final)


async def _safe_execute(
    execution_id: uuid.UUID,
    trigger_kind: str,
    trigger_id: Optional[uuid.UUID] = None,
) -> None:
    """Fire-and-forget wrapper that swallows exceptions (logged)."""
    try:
        await execute_workflow(
            execution_id,
            trigger_kind=trigger_kind,
            trigger_id=trigger_id,
        )
    except Exception:
        logger.exception("background workflow execution failed: %s", execution_id)


# ===========================================================================
# Execution queries (top-level)
# ===========================================================================


@exec_router.get(
    "/projects/{project_id}/workflow-executions",
    response_model=list[WorkflowExecutionResponse],
)
async def list_executions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    workflow_id: Optional[uuid.UUID] = Query(None, alias="workflow_id"),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[WorkflowExecutionResponse]:
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)
    query = select(WorkflowExecution).where(WorkflowExecution.project_id == project_id)
    if workflow_id:
        query = query.where(WorkflowExecution.workflow_id == workflow_id)
    if status_filter:
        query = query.where(WorkflowExecution.status == WorkflowExecutionStatus(status_filter))
    query = query.order_by(WorkflowExecution.created_at.desc()).offset(offset).limit(limit)

    result = await session.execute(query)
    return [_execution_to_response(e) for e in result.scalars().all()]


@exec_router.get(
    "/workflow-executions/{execution_id}",
    response_model=WorkflowExecutionResponse,
)
async def get_execution(
    execution_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WorkflowExecutionResponse:
    result = await session.execute(
        select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="WorkflowExecution 不存在")
    await require_project_role(execution.project_id, Role.VIEWER, principal=user, session=session)
    return _execution_to_response(execution)


@exec_router.get(
    "/workflow-executions/{execution_id}/step-results",
    response_model=list[WorkflowStepResultResponse],
)
async def list_step_results(
    execution_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> list[WorkflowStepResultResponse]:
    result = await session.execute(
        select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="WorkflowExecution 不存在")
    await require_project_role(execution.project_id, Role.VIEWER, principal=user, session=session)

    result = await session.execute(
        select(WorkflowStepResult)
        .where(WorkflowStepResult.execution_id == execution_id)
        .order_by(WorkflowStepResult.step_index.asc(), WorkflowStepResult.created_at.asc())
    )
    return [_step_result_to_response(r) for r in result.scalars().all()]

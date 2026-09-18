"""Action Types & Action Runs API（HIA-70 C1 / M2）

提供 ActionType 的 CRUD，以及 ActionRun 的执行与状态查询。

ActionType 是一种可对项目内对象执行的「操作模板」：
- kind = "function"   → 执行 Python/JS 代码沙箱（代码在 code 字段，runtime 字段指明运行时）
- kind = "webhook"    → 发送 HTTP 请求到外部 URL（config.url 配置）
- kind = "workflow"   → 编排多个子 action（后续 C4 实现）

执行时创建 ActionRun 记录，状态流转：
    PENDING → RUNNING → SUCCESS | FAILED | CANCELED

幂等性：对同一 ActionType + 相同 input_data 的重复 run，
    在 RUNNING 状态内返回已有 run_id（不创建新记录）。
    已完成的 run 允许重跑（idempotent run_id 不同）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.runtime import ActionRun, ActionRunStatus, ActionType, ActionTypeStatus
from src.api.auth import get_current_user, require_project_role, record_audit
from src.db.project import Project
from src.db.identity import Membership, Role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["Actions"])


# =====================================================================
# Pydantic 模型
# =====================================================================


class ActionTypeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    kind: str = Field(..., pattern="^(function|webhook|workflow)$")
    parameters_schema: Optional[dict] = None
    return_schema: Optional[dict] = None
    code: Optional[str] = None          # function 类才有
    runtime: Optional[str] = Field(default="python", max_length=50)  # python | javascript
    config: Optional[dict] = None       # webhook.url, workflow.steps 等


class ActionTypeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(draft|published|deprecated)$")
    parameters_schema: Optional[dict] = None
    return_schema: Optional[dict] = None
    code: Optional[str] = None
    runtime: Optional[str] = Field(None, max_length=50)
    config: Optional[dict] = None


class ActionTypeResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    kind: str
    status: str
    parameters_schema: Optional[dict]
    return_schema: Optional[dict]
    code: Optional[str]
    runtime: Optional[str]
    config: Optional[dict]
    version: int
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class ActionRunCreate(BaseModel):
    action_type_id: uuid.UUID
    input_data: Optional[dict] = Field(default_factory=dict)


class ActionRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    action_type_id: uuid.UUID
    status: str
    input_data: Optional[dict]
    output_data: Optional[dict]
    error: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    triggered_by: Optional[str]
    created_at: str


# =====================================================================
# Helpers
# =====================================================================


def _action_type_to_response(at: ActionType) -> ActionTypeResponse:
    return ActionTypeResponse(
        id=at.id,
        project_id=at.project_id,
        name=at.name,
        description=at.description,
        kind=at.kind,
        status=at.status.value if at.status else "draft",
        parameters_schema=at.parameters_schema,
        return_schema=at.return_schema,
        code=at.code,
        runtime=at.runtime,
        config=at.config,
        version=at.version,
        created_by=at.created_by,
        created_at=at.created_at.isoformat() if at.created_at else "",
        updated_at=at.updated_at.isoformat() if at.updated_at else "",
    )


def _action_run_to_response(ar: ActionRun) -> ActionRunResponse:
    return ActionRunResponse(
        id=ar.id,
        project_id=ar.project_id,
        action_type_id=ar.action_type_id,
        status=ar.status.value if ar.status else "pending",
        input_data=ar.input_data,
        output_data=ar.output_data,
        error=ar.error,
        started_at=ar.started_at.isoformat() if ar.started_at else None,
        completed_at=ar.completed_at.isoformat() if ar.completed_at else None,
        duration_ms=ar.duration_ms,
        triggered_by=ar.triggered_by,
        created_at=ar.created_at.isoformat() if ar.created_at else "",
    )


async def _verify_project_exists(session: AsyncSession, project_id: uuid.UUID) -> Project:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _verify_action_type_exists(
    session: AsyncSession, action_type_id: uuid.UUID, project_id: uuid.UUID
) -> ActionType:
    result = await session.execute(
        select(ActionType).where(
            ActionType.id == action_type_id,
            ActionType.project_id == project_id,
        )
    )
    at = result.scalar_one_or_none()
    if not at:
        raise HTTPException(status_code=404, detail="ActionType 不存在")
    return at


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# ActionType CRUD
# =====================================================================


@router.post(
    "/{project_id}/actions",
    response_model=ActionTypeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_action_type(
    project_id: uuid.UUID,
    data: ActionTypeCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionTypeResponse:
    """在项目中创建 ActionType。

    - kind=function：提供 code + runtime
    - kind=webhook：提供 config.url
    - kind=workflow：后续 C4 实现
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    at = ActionType(
        project_id=project_id,
        name=data.name,
        description=data.description,
        kind=data.kind,
        status=ActionTypeStatus.DRAFT,
        parameters_schema=data.parameters_schema,
        return_schema=data.return_schema,
        code=data.code,
        runtime=data.runtime or "python",
        config=data.config,
        version=1,
        created_by=user.id,
    )
    session.add(at)
    await session.flush()
    await session.refresh(at)

    await record_audit(
        session,
        project_id=project_id,
        actor_id=user.id,
        action="action_type.create",
        resource_type="action_type",
        resource_id=str(at.id),
        details={"name": at.name, "kind": at.kind},
    )

    return _action_type_to_response(at)


@router.get(
    "/{project_id}/actions",
    response_model=list[ActionTypeResponse],
)
async def list_action_types(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status"),
) -> list[ActionTypeResponse]:
    """列出项目下所有 ActionType。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(ActionType).where(ActionType.project_id == project_id)
    if status_filter:
        query = query.where(ActionType.status == ActionTypeStatus(status_filter))
    result = await session.execute(query)
    return [_action_type_to_response(at) for at in result.scalars().all()]


@router.get(
    "/{project_id}/actions/{action_type_id}",
    response_model=ActionTypeResponse,
)
async def get_action_type(
    project_id: uuid.UUID,
    action_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionTypeResponse:
    """获取单个 ActionType。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)
    at = await _verify_action_type_exists(session, action_type_id, project_id)
    return _action_type_to_response(at)


@router.patch(
    "/{project_id}/actions/{action_type_id}",
    response_model=ActionTypeResponse,
)
async def update_action_type(
    project_id: uuid.UUID,
    action_type_id: uuid.UUID,
    data: ActionTypeUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionTypeResponse:
    """更新 ActionType（仅 DRAFT 状态允许编辑 code/runtime）。"""
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)
    at = await _verify_action_type_exists(session, action_type_id, project_id)

    if data.name is not None:
        at.name = data.name
    if data.description is not None:
        at.description = data.description
    if data.status is not None:
        at.status = ActionTypeStatus(data.status)
    if data.parameters_schema is not None:
        at.parameters_schema = data.parameters_schema
    if data.return_schema is not None:
        at.return_schema = data.return_schema
    if data.runtime is not None:
        at.runtime = data.runtime

    # code 只能在 DRAFT 状态修改
    if data.code is not None and at.status == ActionTypeStatus.DRAFT:
        at.code = data.code
        at.version += 1
    if data.config is not None:
        at.config = data.config

    await session.flush()
    await session.refresh(at)

    await record_audit(
        session,
        project_id=project_id,
        actor_id=user.id,
        action="action_type.update",
        resource_type="action_type",
        resource_id=str(at.id),
        details={"name": at.name},
    )

    return _action_type_to_response(at)


@router.delete(
    "/{project_id}/actions/{action_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_action_type(
    project_id: uuid.UUID,
    action_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    """删除 ActionType（如果有 RUNNING 的 run 不允许删除）。"""
    await require_project_role(project_id, Role.OWNER, principal=user, session=session)
    at = await _verify_action_type_exists(session, action_type_id, project_id)

    # 检查是否有 RUNNING 的 run
    running = await session.execute(
        select(ActionRun).where(
            ActionRun.action_type_id == action_type_id,
            ActionRun.status == ActionRunStatus.RUNNING,
        )
    )
    if running.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="该 ActionType 有 RUNNING 中的执行，不允许删除",
        )

    await session.delete(at)
    await session.flush()

    await record_audit(
        session,
        project_id=project_id,
        actor_id=user.id,
        action="action_type.delete",
        resource_type="action_type",
        resource_id=str(action_type_id),
        details={"name": at.name},
    )


# =====================================================================
# ActionRun 执行
# =====================================================================


@router.post(
    "/{project_id}/actions/{action_type_id}/run",
    response_model=ActionRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_action(
    project_id: uuid.UUID,
    action_type_id: uuid.UUID,
    data: ActionRunCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionRunResponse:
    """执行 ActionType，异步创建 ActionRun 并调度执行。

    幂等性：对同一 action_type_id + input_data，若存在 RUNNING 的 run，
    返回该 run 而不创建新记录。
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)
    at = await _verify_action_type_exists(session, action_type_id, project_id)

    # 检查是否已存在 RUNNING 的 run（幂等）
    existing = await session.execute(
        select(ActionRun).where(
            ActionRun.action_type_id == action_type_id,
            ActionRun.status == ActionRunStatus.RUNNING,
        )
    )
    running = existing.scalar_one_or_none()
    if running and (running.input_data or {}) == (data.input_data or {}):
        return _action_run_to_response(running)

    run = ActionRun(
        project_id=project_id,
        action_type_id=action_type_id,
        status=ActionRunStatus.PENDING,
        input_data=data.input_data or {},
        triggered_by=str(user.id),
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    # 异步执行（HIA-78 C2 沙箱实现）
    asyncio.create_task(_execute_action(run.id, at, session))

    await record_audit(
        session,
        project_id=project_id,
        actor_id=user.id,
        action="action_run.create",
        resource_type="action_run",
        resource_id=str(run.id),
        details={"action_type_id": str(action_type_id), "kind": at.kind},
    )

    return _action_run_to_response(run)


async def _execute_action(run_id: uuid.UUID, at: ActionType, session: AsyncSession) -> None:
    """在后台任务中执行 action。

    HIA-78 C2 实现：
    - function/python：使用 subprocess 执行 Python 代码片段（超时 30s）
    - function/javascript：使用 subprocess 执行 node（超时 30s）
    - webhook：httpx async POST
    - workflow：串联子 action（C4 实现）
    """
    import httpx
    from src.db.connection import async_session_factory

    started = _now_utc()

    async def _get_run_input() -> dict:
        async with async_session_factory() as s:
            result = await s.execute(select(ActionRun).where(ActionRun.id == run_id))
            run = result.scalar_one_or_none()
            return run.input_data if run else {}

    async def _mark_running() -> None:
        async with async_session_factory() as s:
            result = await s.execute(select(ActionRun).where(ActionRun.id == run_id))
            run = result.scalar_one_or_none()
            if run:
                run.status = ActionRunStatus.RUNNING
                run.started_at = started
                await s.flush()

    async def _mark_done(status: ActionRunStatus, output: Optional[dict] = None, error: Optional[str] = None) -> None:
        async with async_session_factory() as s:
            result = await s.execute(select(ActionRun).where(ActionRun.id == run_id))
            run = result.scalar_one_or_none()
            if run:
                run.status = status
                run.output_data = output
                run.error = error
                completed = _now_utc()
                run.completed_at = completed
                run.duration_ms = int((completed - started).total_seconds() * 1000)
                await s.flush()

    await _mark_running()

    # 从 ActionRun 记录中获取 input_data（ActionType 没有 input_data 字段）
    input_data = await _get_run_input()

    try:
        if at.kind == "webhook":
            url = (at.config or {}).get("url", "")
            method = (at.config or {}).get("method", "POST").upper()
            timeout = (at.config or {}).get("timeout", 15)

            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                fn = getattr(client, method.lower(), client.post)
                resp = await fn(url, json=input_data)
            await _mark_done(ActionRunStatus.SUCCESS, output={"status_code": resp.status_code, "body": resp.text})
            return

        elif at.kind == "function":
            code = at.code or ""
            runtime = (at.runtime or "python").lower()

            # HIA-78 C2：使用真正的 sandbox 模块执行
            result = await _sandbox_execute(code, runtime, input_data or {}, at.config)
            await _mark_done(ActionRunStatus.SUCCESS, output=result)
            return

        else:
            # workflow — C4 占位
            await _mark_done(
                ActionRunStatus.FAILED,
                error=f"kind={at.kind} 执行尚未实现（C4）",
            )
            return

    except asyncio.TimeoutError:
        await _mark_done(ActionRunStatus.FAILED, error="执行超时（30s）")
    except Exception as exc:
        logger.exception("Action execution failed: run_id=%s", run_id)
        await _mark_done(ActionRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


async def _sandbox_execute(
    code: str, runtime: str, input_data: dict, config: Optional[dict] = None
) -> dict:
    """HIA-78 C2: 使用 sandbox 模块执行代码。

    真正的 sandbox：subprocess + timeout + (Unix) rlimit。
    - Python: execute_python() — subprocess 隔离，timeout 强制
    - JavaScript: execute_javascript() — Node subprocess，timeout 强制
    """
    from src.runtime.sandbox import (
        execute_python,
        execute_javascript,
        SandboxError as SBXError,
    )

    cfg = config or {}
    timeout_s = cfg.get("timeout_s", 30)

    try:
        if runtime == "python":
            result = execute_python(
                code=code,
                input_data=input_data,
                secrets=cfg.get("secrets"),
                timeout=timeout_s,
            )
            return result.to_dict()

        elif runtime == "javascript":
            result = execute_javascript(
                code=code,
                input_data=input_data,
                timeout=timeout_s,
            )
            return result.to_dict()

        else:
            return {
                "stderr": f"Unknown runtime: {runtime}",
                "result": None,
            }

    except SBXError as exc:
        return {
            "stderr": f"[{exc.kind}] {exc.message}",
            "result": None,
            "error_kind": exc.kind,
        }


# =====================================================================
# ActionRun 查询 / 管理
# =====================================================================


@router.get(
    "/{project_id}/action-runs",
    response_model=list[ActionRunResponse],
)
async def list_action_runs(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status"),
    action_type_id: Optional[uuid.UUID] = Query(None, alias="action_type_id"),
) -> list[ActionRunResponse]:
    """列出项目下 ActionRun。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(ActionRun).where(ActionRun.project_id == project_id)
    if status_filter:
        query = query.where(ActionRun.status == ActionRunStatus(status_filter))
    if action_type_id:
        query = query.where(ActionRun.action_type_id == action_type_id)
    query = query.order_by(ActionRun.created_at.desc())

    result = await session.execute(query)
    return [_action_run_to_response(ar) for ar in result.scalars().all()]


@router.get(
    "/action-runs/{run_id}",
    response_model=ActionRunResponse,
)
async def get_action_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionRunResponse:
    """获取单个 ActionRun（不限项目，actor 自动校验）。"""
    result = await session.execute(select(ActionRun).where(ActionRun.id == run_id))
    ar = result.scalar_one_or_none()
    if not ar:
        raise HTTPException(status_code=404, detail="ActionRun 不存在")

    # 简单权限校验：用户必须是项目成员
    await require_project_role(ar.project_id, Role.VIEWER, principal=user, session=session)
    return _action_run_to_response(ar)


@router.post(
    "/action-runs/{run_id}/cancel",
    response_model=ActionRunResponse,
)
async def cancel_action_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> ActionRunResponse:
    """取消 RUNNING 中的 ActionRun（幂等：已完成的不报错）。"""
    result = await session.execute(select(ActionRun).where(ActionRun.id == run_id))
    ar = result.scalar_one_or_none()
    if not ar:
        raise HTTPException(status_code=404, detail="ActionRun 不存在")

    await require_project_role(ar.project_id, Role.EDITOR, principal=user, session=session)

    if ar.status == ActionRunStatus.RUNNING:
        ar.status = ActionRunStatus.CANCELED
        ar.completed_at = _now_utc()
        ar.error = "Cancelled by user"
        await session.flush()
        await session.refresh(ar)

    await record_audit(
        session,
        project_id=ar.project_id,
        actor_id=user.id,
        action="action_run.cancel",
        resource_type="action_run",
        resource_id=str(ar.id),
        details={"previous_status": str(ar.status.value)},
    )

    return _action_run_to_response(ar)

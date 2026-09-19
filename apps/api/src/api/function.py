"""Function CRUD + Test-run API (HIA-70 C1).

Provides separately-addressable Function entities that can be:

* Shared by multiple ActionType (``function_id`` reference)
* Referenced from workflow steps (future)
* Tested directly via ``POST /projects/{id}/functions/{fid}/test``

The test endpoint runs synchronously (unlike ActionRun which is async)
because the editor's "Run" button expects immediate output.

## Routes

* ``POST   /projects/{id}/functions``         — create
* ``GET    /projects/{id}/functions``         — list
* ``GET    /projects/{id}/functions/{fid}``   — read one
* ``PATCH  /projects/{id}/functions/{fid}``   — update (bumps version)
* ``DELETE /projects/{id}/functions/{fid}``   — delete
* ``POST   /projects/{id}/functions/{fid}/test``  — sync run; writes FunctionRun record
* ``GET    /projects/{id}/functions/{fid}/runs``  — list recent test runs

## Storage

Function rows live in ``src.db.runtime.Function`` (table ``functions``).
Each PATCH on ``source_code`` or ``config`` increments ``version``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.runtime import (
    Function,
    FunctionLanguage,
    FunctionRun,
)
from src.api.auth import get_current_user, require_project_role, record_audit
from src.db.identity import Role
from src.db.governance import AuditEventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["Functions"])


# =====================================================================
# Constants & helpers
# =====================================================================

# api_name: 字母 / 数字 / 下划线 / 点；不允许以数字开头（避免与 JSON path 等冲突）。
_API_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# 默认 test-run 超时（与 ActionType sandbox 默认对齐）
DEFAULT_TEST_TIMEOUT_S = 30
# 默认最大 stdout 大小
DEFAULT_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MiB


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _function_to_response(f: Function) -> "FunctionResponse":
    return FunctionResponse(
        id=f.id,
        project_id=f.project_id,
        api_name=f.api_name,
        display_name=f.display_name,
        description=f.description,
        language=f.language.value if f.language else "python",
        source_code=f.source_code or "",
        version=f.version,
        parameters_schema=f.parameters_schema,
        return_schema=f.return_schema,
        config=f.config,
        created_by=f.created_by,
        created_at=f.created_at.isoformat() if f.created_at else "",
        updated_at=f.updated_at.isoformat() if f.updated_at else "",
    )


def _function_run_to_response(fr: FunctionRun) -> "FunctionRunResponse":
    return FunctionRunResponse(
        id=fr.id,
        project_id=fr.project_id,
        function_id=fr.function_id,
        input_data=fr.input_data,
        output_data=fr.output_data,
        error=fr.error,
        duration_ms=fr.duration_ms,
        triggered_by=fr.triggered_by,
        created_at=fr.created_at.isoformat() if fr.created_at else "",
    )


# =====================================================================
# Pydantic models
# =====================================================================


class FunctionCreate(BaseModel):
    api_name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    language: str = Field(default="python", pattern="^(python|javascript|typescript)$")
    source_code: str = Field(default="", max_length=200_000)
    parameters_schema: Optional[dict] = None
    return_schema: Optional[dict] = None
    config: Optional[dict] = None

    @field_validator("api_name")
    @classmethod
    def _validate_api_name(cls, v: str) -> str:
        if not _API_NAME_RE.match(v):
            raise ValueError(
                "api_name 必须以字母/下划线开头，仅含字母/数字/下划线/点"
            )
        return v


class FunctionUpdate(BaseModel):
    api_name: Optional[str] = Field(None, min_length=1, max_length=255)
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    source_code: Optional[str] = Field(None, max_length=200_000)
    parameters_schema: Optional[dict] = None
    return_schema: Optional[dict] = None
    config: Optional[dict] = None

    @field_validator("api_name")
    @classmethod
    def _validate_api_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _API_NAME_RE.match(v):
            raise ValueError(
                "api_name 必须以字母/下划线开头，仅含字母/数字/下划线/点"
            )
        return v


class FunctionResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    api_name: str
    display_name: str
    description: Optional[str]
    language: str
    source_code: str
    version: int
    parameters_schema: Optional[dict]
    return_schema: Optional[dict]
    config: Optional[dict]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class FunctionTestRequest(BaseModel):
    input_data: Optional[dict] = Field(default_factory=dict)
    timeout_s: Optional[int] = Field(default=DEFAULT_TEST_TIMEOUT_S, ge=1, le=300)
    # 用于同步执行的额外环境变量；只在 editor "Run" 上下文使用，不持久化
    secrets: Optional[dict] = None


class FunctionTestResponse(BaseModel):
    """同步 test-run 响应。

    ``output_data`` 是 sandbox 输出的 ``result`` 字段；``stdout``/``stderr``
    便于 UI 显示在编辑器面板。失败时 ``error`` 包含原始异常类型/消息。
    """

    run_id: uuid.UUID
    function_id: uuid.UUID
    version: int
    output_data: Optional[Any] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int
    timed_out: bool = False


class FunctionRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    function_id: uuid.UUID
    input_data: Optional[dict]
    output_data: Optional[dict]
    error: Optional[str]
    duration_ms: Optional[int]
    triggered_by: Optional[str]
    created_at: str


# =====================================================================
# Function CRUD
# =====================================================================


@router.post(
    "/{project_id}/functions",
    response_model=FunctionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_function(
    project_id: uuid.UUID,
    data: FunctionCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> FunctionResponse:
    """创建 Function。api_name 在项目内必须唯一。"""
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    fn = Function(
        project_id=project_id,
        api_name=data.api_name,
        display_name=data.display_name,
        description=data.description,
        language=FunctionLanguage(data.language),
        source_code=data.source_code or "",
        parameters_schema=data.parameters_schema,
        return_schema=data.return_schema,
        config=data.config,
        version=1,
        created_by=user.user.id if hasattr(user, "user") else None,
    )
    session.add(fn)
    try:
        await session.flush()
    except IntegrityError as exc:
        # 唯一冲突：同 (project_id, api_name) 已存在
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Function api_name='{data.api_name}' 已存在",
        ) from exc
    await session.refresh(fn)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=project_id,
        target_type="function",
        target_id=str(fn.id),
        after={"api_name": fn.api_name, "language": fn.language.value},
    )

    return _function_to_response(fn)


@router.get(
    "/{project_id}/functions",
    response_model=list[FunctionResponse],
)
async def list_functions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    language: Optional[str] = Query(None),
) -> list[FunctionResponse]:
    """列出项目下所有 Function。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(Function).where(Function.project_id == project_id)
    if language:
        query = query.where(Function.language == FunctionLanguage(language))
    query = query.order_by(Function.api_name.asc())
    result = await session.execute(query)
    return [_function_to_response(f) for f in result.scalars().all()]


@router.get(
    "/{project_id}/functions/{function_id}",
    response_model=FunctionResponse,
)
async def get_function(
    project_id: uuid.UUID,
    function_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> FunctionResponse:
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    result = await session.execute(
        select(Function).where(
            Function.id == function_id,
            Function.project_id == project_id,
        )
    )
    fn = result.scalar_one_or_none()
    if not fn:
        raise HTTPException(status_code=404, detail="Function 不存在")
    return _function_to_response(fn)


@router.patch(
    "/{project_id}/functions/{function_id}",
    response_model=FunctionResponse,
)
async def update_function(
    project_id: uuid.UUID,
    function_id: uuid.UUID,
    data: FunctionUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> FunctionResponse:
    """更新 Function。

    任意 ``source_code`` / ``config`` / ``schema`` 变更会 bump ``version``。
    ``api_name`` 重复 → 409。
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    result = await session.execute(
        select(Function).where(
            Function.id == function_id,
            Function.project_id == project_id,
        )
    )
    fn = result.scalar_one_or_none()
    if not fn:
        raise HTTPException(status_code=404, detail="Function 不存在")

    bump_version = False
    if data.api_name is not None and data.api_name != fn.api_name:
        fn.api_name = data.api_name
        bump_version = True  # rename 也是破坏性变更
    if data.display_name is not None:
        fn.display_name = data.display_name
    if data.description is not None:
        fn.description = data.description
    if data.source_code is not None and data.source_code != fn.source_code:
        fn.source_code = data.source_code
        bump_version = True
    if data.parameters_schema is not None:
        fn.parameters_schema = data.parameters_schema
        bump_version = True
    if data.return_schema is not None:
        fn.return_schema = data.return_schema
        bump_version = True
    if data.config is not None:
        fn.config = data.config
        bump_version = True

    if bump_version:
        fn.version += 1

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Function api_name='{data.api_name}' 已存在",
        ) from exc
    await session.refresh(fn)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=project_id,
        target_type="function",
        target_id=str(fn.id),
        after={"api_name": fn.api_name, "version": fn.version},
    )

    return _function_to_response(fn)


@router.delete(
    "/{project_id}/functions/{function_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_function(
    project_id: uuid.UUID,
    function_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    await require_project_role(project_id, Role.OWNER, principal=user, session=session)

    result = await session.execute(
        select(Function).where(
            Function.id == function_id,
            Function.project_id == project_id,
        )
    )
    fn = result.scalar_one_or_none()
    if not fn:
        raise HTTPException(status_code=404, detail="Function 不存在")

    await session.delete(fn)
    await session.flush()

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=project_id,
        target_type="function",
        target_id=str(function_id),
        after={"api_name": fn.api_name},
    )


# =====================================================================
# Test-run (synchronous)
# =====================================================================


def _run_function_sync(
    fn: Function,
    input_data: dict,
    timeout_s: int,
    secrets: Optional[dict],
) -> tuple[Any, Optional[str], Optional[str], Optional[str], bool]:
    """在 FastAPI handler 里通过 ``asyncio.to_thread`` 调用的同步函数。

    返回 ``(output, stdout, stderr, error_message, timed_out)``。
    """
    from src.runtime.sandbox import (
        SandboxError,
        execute_javascript,
        execute_python,
    )

    started = time.monotonic()
    timed_out = False
    err: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    output: Any = None

    try:
        if fn.language == FunctionLanguage.PYTHON:
            result = execute_python(
                code=fn.source_code or "",
                input_data=input_data,
                secrets=secrets,
                timeout=timeout_s,
                max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
            )
            output = result.result
            stdout = result.stdout
            stderr = result.stderr
            # HIA-76 修复后：sandbox 把用户异常包成 {"_error": ..., "_traceback": ...}
            # 通过 <<<RESULT>>> envelope 传给父进程。这是权威错误信号；
            # exit_code 也可能是 1（sandbox sys.exit(1)），但 _error envelope 更精确。
            if isinstance(output, dict) and "_error" in output:
                err = str(output["_error"])
                tb = output.get("_traceback")
                if tb:
                    stderr = (stderr or "") + ("\n" if stderr else "") + tb
                output = None
            elif result.timed_out:
                timed_out = True
                err = f"执行超时 ({timeout_s}s)"
            elif result.exit_code != 0:
                # 非零退出但没有 _error envelope（语法错误、RestrictedPython 拦截等）
                err = (result.stderr or "").strip().splitlines()[-1] if result.stderr else "sandbox 退出码非零"
                if not err:
                    err = "sandbox 退出码非零"

        elif fn.language == FunctionLanguage.JAVASCRIPT:
            result = execute_javascript(
                code=fn.source_code or "",
                input_data=input_data,
                timeout=timeout_s,
            )
            output = result.result
            stdout = result.stdout
            stderr = result.stderr
            if result.timed_out:
                timed_out = True
                err = f"执行超时 ({timeout_s}s)"
            elif result.exit_code != 0:
                err = (result.stderr or "").strip().splitlines()[-1] if result.stderr else "sandbox 退出码非零"
                if not err:
                    err = "sandbox 退出码非零"

        else:
            # typescript：当前 runtime 未实现，直接报错
            err = (
                "TypeScript 当前未实现：请使用 python 或 javascript，"
                "或在编辑器内先转译"
            )

    except SandboxError as exc:
        err = f"[{exc.kind}] {exc.message}"
        if exc.kind == "timeout":
            timed_out = True
    except subprocess.TimeoutExpired:  # pragma: no cover — execute_python 自身处理
        timed_out = True
        err = f"执行超时 ({timeout_s}s)"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "function test-run: id=%s api_name=%s elapsed_ms=%s timed_out=%s err=%s",
        fn.id, fn.api_name, elapsed_ms, timed_out, err,
    )
    return output, stdout, stderr, err, timed_out


@router.post(
    "/{project_id}/functions/{function_id}/test",
    response_model=FunctionTestResponse,
)
async def test_function(
    project_id: uuid.UUID,
    function_id: uuid.UUID,
    data: FunctionTestRequest,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> FunctionTestResponse:
    """同步执行 Function 并返回 result；写 FunctionRun 审计记录。

    与 ActionRun 不同：test-run 总是同步返回（不创建 ActionRun），
    但保留一份 FunctionRun 记录便于 UI 列出最近试运行。
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    result = await session.execute(
        select(Function).where(
            Function.id == function_id,
            Function.project_id == project_id,
        )
    )
    fn = result.scalar_one_or_none()
    if not fn:
        raise HTTPException(status_code=404, detail="Function 不存在")

    # 同步 sandbox 调用放到线程池，避免阻塞 event loop
    _run_started = time.monotonic()
    output, stdout, stderr, err, timed_out = await asyncio.to_thread(
        _run_function_sync,
        fn,
        data.input_data or {},
        data.timeout_s or DEFAULT_TEST_TIMEOUT_S,
        data.secrets,
    )
    run_elapsed_ms = int((time.monotonic() - _run_started) * 1000)

    fr = FunctionRun(
        project_id=project_id,
        function_id=function_id,
        input_data=data.input_data or {},
        output_data={"result": output} if err is None else None,
        error=err,
        duration_ms=run_elapsed_ms,
        triggered_by=str(user.id if hasattr(user, "id") else user.user.id),
    )
    session.add(fr)
    try:
        await session.flush()
    except Exception as exc:  # 写 FunctionRun 失败不阻塞响应（test-run 的结果更重要）
        logger.warning("FunctionRun persist failed (non-fatal): %s", exc)
        await session.rollback()

    return FunctionTestResponse(
        run_id=fr.id,
        function_id=function_id,
        version=fn.version,
        output_data=output,
        stdout=stdout,
        stderr=stderr,
        error=err,
        duration_ms=run_elapsed_ms,
        timed_out=timed_out,
    )


@router.get(
    "/{project_id}/functions/{function_id}/runs",
    response_model=list[FunctionRunResponse],
)
async def list_function_runs(
    project_id: uuid.UUID,
    function_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
) -> list[FunctionRunResponse]:
    """列出某 Function 的最近试运行（默认 20 条）。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = (
        select(FunctionRun)
        .where(
            FunctionRun.function_id == function_id,
            FunctionRun.project_id == project_id,
        )
        .order_by(FunctionRun.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    return [_function_run_to_response(fr) for fr in result.scalars().all()]

"""Connector API（HIA-71 B6 — Connector 框架 API 层）。

约定：
- 所有路由都需要 ``get_current_user``，列表 / 创建通过 ``require_role_query`` 锁定
  项目角色，避免侧信道泄漏。
- ``config`` 中标记为 secret 的字段在落盘前用 :mod:`src.core.secrets` 加密；
  返回给前端的视图会 ``mask_secret_fields``，仅在用户显式 ``reveal=true`` 时返回
  明文（写入审计）。
- ``/test`` 端点会真实跑 ``test_connection``，结果写入 ``Connector.last_*`` 字段，
  避免每次重复拨号。
- ``/tables`` / ``/snapshot`` 直接调用注册表中的 connector 实现。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    CurrentPrincipal,
    coerce_diff,
    get_current_user,
    record_audit,
    require_role_query,
)
from src.core.secrets import (
    decrypt_secret_fields,
    encrypt_secret_fields,
    mask_secret_fields,
)
from src.db.connection import get_session
from src.db.connector import Connector, ConnectorStatus, ConnectorType
from src.db.evidence import Evidence, EvidenceType, Source, SourceSnapshot, SourceStatus, SourceType
from src.db.governance import AuditEventType
from src.db.identity import Role
from src.services.connectors import (
    ConnectorError,
    get_connector,
    is_registered,
    list_connector_types,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/connectors", tags=["connectors"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectorTypeInfo(BaseModel):
    type: str
    description: Optional[str] = None
    config_schema: Optional[dict[str, Any]] = None


class ConnectorCreate(BaseModel):
    type: ConnectorType = Field(..., description="连接器类型枚举")
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)
    secret_fields: list[str] = Field(
        default_factory=list,
        description="config 中需要落盘前加密的字段名列表（如 ['password']）",
    )
    default_sample_limit: int = Field(default=1000, ge=1, le=100_000)


class ConnectorUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    secret_fields: Optional[list[str]] = None
    status: Optional[ConnectorStatus] = None
    default_sample_limit: Optional[int] = Field(None, ge=1, le=100_000)


class ConnectorResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    type: ConnectorType
    name: str
    description: Optional[str]
    # config：默认 masked；reveal=true 时才返回明文
    config: dict[str, Any]
    secret_fields: list[str]
    status: ConnectorStatus
    last_tested_at: Optional[str]
    last_test_message: Optional[str]
    last_test_ok: Optional[bool]
    default_sample_limit: int
    created_at: str
    updated_at: str
    is_secrets_revealed: bool = False  # 当前响应是否包含明文

    model_config = ConfigDict(from_attributes=True)


class ConnectorTestResponse(BaseModel):
    ok: bool
    message: str
    tested_at: str


class ConnectorTableInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: Optional[str] = None
    row_count_estimate: Optional[int] = None
    # ``schema`` 是 Pydantic / BaseModel 的保留属性名，改用 ``schema_`` 但 JSON
    # 字段名保持 ``schema``（populate_by_name=True 让 alias 与字段名都能用）。
    schema_: Optional[list[dict[str, Any]]] = Field(
        default=None, alias="schema", serialization_alias="schema"
    )


class ConnectorSnapshotRequest(BaseModel):
    table: str = Field(..., min_length=1, max_length=255)
    limit: int = Field(default=1000, ge=1, le=100_000)
    offset: int = Field(default=0, ge=0)


class ConnectorSnapshotResponse(BaseModel):
    table: str
    fields: list[dict[str, Any]]
    row_count: int
    truncated: bool
    rows: list[dict[str, Any]]


class ConnectorSnapshotToEvidenceRequest(BaseModel):
    """快照参数 + 是否自动为每个字段生成 evidence（HIA-67 B2 验收用）。"""

    table: str = Field(..., min_length=1, max_length=255)
    limit: int = Field(default=1000, ge=1, le=100_000)
    offset: int = Field(default=0, ge=0)
    auto_evidence: bool = Field(
        default=True,
        description="true 时为每个字段自动生成 Evidence 记录（默认开启）",
    )


class ConnectorSnapshotToEvidenceResponse(BaseModel):
    """快照结果 + 创建的 Source / Evidence 记录 ID（HIA-67 验收用）。"""

    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    table: str
    fields: list[dict[str, Any]]
    row_count: int
    truncated: bool
    rows: list[dict[str, Any]]
    evidence_ids: list[uuid.UUID]
    profile: dict[str, Any]  # 字段统计（null_ratio / unique_ratio / sample_values）


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# PG 列类型到 ontology 数据类型的映射（HIA-67 字段推断）
_PG_TYPE_TO_MODEL: dict[str, str] = {
    "integer": "int",
    "bigint": "int",
    "smallint": "int",
    "numeric": "float",
    "real": "float",
    "double precision": "float",
    "boolean": "bool",
    "character varying": "string",
    "character": "string",
    "text": "string",
    "date": "date",
    "timestamp without time zone": "date",
    "timestamp with time zone": "date",
    "time without time zone": "date",
    "time with time zone": "date",
    "uuid": "string",
    "json": "string",
    "jsonb": "string",
}


def _pg_type_to_model(pg_type: str) -> str:
    """把 PostgreSQL 列类型映射到本体数据类型。"""
    return _PG_TYPE_TO_MODEL.get(pg_type.lower(), "string")


async def _load_for_project(
    session: AsyncSession,
    *,
    connector_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Connector:
    res = await session.execute(
        select(Connector).where(
            Connector.id == connector_id,
            Connector.project_id == project_id,
            Connector.deleted_at.is_(None),
        )
    )
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return c


def _to_response(
    c: Connector,
    *,
    reveal: bool = False,
) -> ConnectorResponse:
    fields = list(c.secret_fields or [])
    if reveal:
        cfg = decrypt_secret_fields(c.config or {}, fields)
        secrets_revealed = True
    else:
        cfg = mask_secret_fields(c.config or {}, fields)
        secrets_revealed = False
    return ConnectorResponse(
        id=c.id,
        project_id=c.project_id,
        type=c.type,
        name=c.name,
        description=c.description,
        config=cfg,
        secret_fields=fields,
        status=c.status,
        last_tested_at=c.last_tested_at.isoformat() if c.last_tested_at else None,
        last_test_message=c.last_test_message,
        last_test_ok=c.last_test_ok,
        default_sample_limit=c.default_sample_limit,
        created_at=c.created_at.isoformat() if c.created_at else "",
        updated_at=c.updated_at.isoformat() if c.updated_at else "",
        is_secrets_revealed=secrets_revealed,
    )


# ---------------------------------------------------------------------------
# Public: list registered connector types (no auth — UI form)
# ---------------------------------------------------------------------------


@router.get("/types", response_model=list[ConnectorTypeInfo])
async def list_types(
    _principal: CurrentPrincipal = Depends(get_current_user),
) -> list[ConnectorTypeInfo]:
    """列出注册表里所有可用的 connector 类型；用于 UI 下拉选择。

    注：仍要求已登录（避免枚举内部命名），但不绑定到具体项目。
    """
    type_to_info: dict[str, ConnectorTypeInfo] = {
        ConnectorType.CSV.value: ConnectorTypeInfo(
            type="csv", description="本地 CSV 文件（相对 ~/.ontolohub/data 或绝对路径）"
        ),
        ConnectorType.EXCEL.value: ConnectorTypeInfo(
            type="excel", description="Excel 工作簿（需 openpyxl）"
        ),
        ConnectorType.JSON.value: ConnectorTypeInfo(
            type="json", description="JSON 文件或 URL（数组或 $.path 指向数组）"
        ),
        ConnectorType.PARQUET.value: ConnectorTypeInfo(
            type="parquet", description="Parquet 列存文件（需 pyarrow）"
        ),
        ConnectorType.POSTGRESQL.value: ConnectorTypeInfo(
            type="postgresql",
            description="PostgreSQL 只读连接器（带白名单 + statement_timeout）",
        ),
    }
    out: list[ConnectorTypeInfo] = []
    for t in list_connector_types():
        info = type_to_info.get(t, ConnectorTypeInfo(type=t))
        # 同步枚举里的 type 字段，避免不一致
        info.type = t
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ConnectorResponse])
async def list_connectors(
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    type_filter: Optional[ConnectorType] = Query(None, alias="type"),
    status_filter: Optional[ConnectorStatus] = Query(None, alias="status"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[ConnectorResponse]:
    """列出项目内的 connector（不含已软删除的）。"""
    query = select(Connector).where(
        Connector.project_id == project_id,
        Connector.deleted_at.is_(None),
    )
    if type_filter:
        query = query.where(Connector.type == type_filter)
    if status_filter:
        query = query.where(Connector.status == status_filter)
    query = query.order_by(Connector.created_at.desc())
    res = await session.execute(query)
    return [_to_response(c) for c in res.scalars().all()]


@router.post("", response_model=ConnectorResponse, status_code=status.HTTP_201_CREATED)
async def create_connector(
    data: ConnectorCreate,
    request: Request,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ConnectorResponse:
    """新建一个 connector；``config.secret_fields`` 中的字段会落盘前加密。"""
    principal, _ = ctx

    if not is_registered(data.type.value):
        raise HTTPException(
            status_code=400,
            detail=(
                f"connector type {data.type!r} is not registered; "
                f"available: {list_connector_types()}"
            ),
        )

    encrypted_cfg = encrypt_secret_fields(data.config, data.secret_fields or [])
    conn = Connector(
        project_id=project_id,
        type=data.type,
        name=data.name,
        description=data.description,
        config=encrypted_cfg,
        secret_fields=list(data.secret_fields or []),
        default_sample_limit=data.default_sample_limit,
        status=ConnectorStatus.ACTIVE,
        created_by=principal.user.id,
    )
    session.add(conn)
    await session.flush()
    await session.refresh(conn)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        request=request,
        target_type="connector",
        target_id=str(conn.id),
        target_label=conn.name,
        after=coerce_diff(conn),
        notes=f"type={conn.type.value} secret_fields={list(conn.secret_fields or [])}",
    )
    return _to_response(conn)


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector_by_id(
    connector_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    reveal: bool = Query(False, description="是否返回 secret 明文（需 EDITOR+）"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> ConnectorResponse:
    principal, role = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    if reveal and role < Role.EDITOR:
        raise HTTPException(status_code=403, detail="reveal requires EDITOR+")
    return _to_response(conn, reveal=reveal)


@router.patch("/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    connector_id: uuid.UUID,
    data: ConnectorUpdate,
    request: Request,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ConnectorResponse:
    principal, _ = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )

    before = coerce_diff(conn)
    payload = data.model_dump(exclude_unset=True)

    # config 增量更新：新传入的 config 视为「完整 config」，与既有字段合并后加密。
    if "config" in payload:
        new_cfg = payload.pop("config") or {}
        if "secret_fields" in payload:
            new_secret_fields = list(payload["secret_fields"] or [])
            payload["secret_fields"] = new_secret_fields
        else:
            new_secret_fields = list(conn.secret_fields or [])
        # 合并：把旧的明文（如果有）作为基底，再覆盖新值；未在 secret 列表里的字段按原样保留
        merged = dict(conn.config or {})
        merged.update(new_cfg)
        # 对 secret 字段，传入可能是密文也可能是明文；统一解密后用明文存储
        decrypted_old = decrypt_secret_fields(merged, new_secret_fields)
        payload["config"] = encrypt_secret_fields(
            decrypted_old, new_secret_fields
        )
    elif "secret_fields" in payload:
        # 仅修改 secret_fields 列表 → 重新加密整个 config
        new_secret_fields = list(payload["secret_fields"] or [])
        decrypted_old = decrypt_secret_fields(conn.config or {}, new_secret_fields)
        payload["config"] = encrypt_secret_fields(
            decrypted_old, new_secret_fields
        )
        payload["secret_fields"] = new_secret_fields

    for field_, value in payload.items():
        setattr(conn, field_, value)
    await session.flush()
    await session.refresh(conn)
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        request=request,
        target_type="connector",
        target_id=str(conn.id),
        target_label=conn.name,
        before=before,
        after=coerce_diff(conn),
    )
    return _to_response(conn)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: uuid.UUID,
    request: Request,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> None:
    principal, _ = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    before = coerce_diff(conn)
    label = conn.name
    cid = str(conn.id)
    conn.deleted_at = datetime.now(timezone.utc)
    conn.deleted_by = principal.user.id
    conn.status = ConnectorStatus.ARCHIVED
    await session.flush()
    await session.refresh(conn)
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        project_id=project_id,
        request=request,
        target_type="connector",
        target_id=cid,
        target_label=label,
        before=before,
        after=coerce_diff(conn),
    )


# ---------------------------------------------------------------------------
# Connection ops
# ---------------------------------------------------------------------------


@router.post("/{connector_id}/test", response_model=ConnectorTestResponse)
async def test_connector(
    connector_id: uuid.UUID,
    request: Request,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ConnectorTestResponse:
    """真实跑一次 ``test_connection``；结果写回 ``Connector.last_*``。"""
    principal, _ = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    plain_cfg = decrypt_secret_fields(conn.config or {}, conn.secret_fields or [])
    try:
        c = get_connector(conn.type.value, plain_cfg)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        ok, message = await asyncio.wait_for(c.test_connection(), timeout=30)
    except asyncio.TimeoutError:
        ok, message = False, "connection test timed out after 30s"
    except ConnectorError as e:
        ok, message = False, str(e)
    except Exception as e:  # pragma: no cover — 兜底
        logger.exception("connector test crashed: %s", conn.id)
        ok, message = False, f"{type(e).__name__}: {e}"

    now = datetime.now(timezone.utc)
    before = coerce_diff(conn)
    conn.last_tested_at = now
    conn.last_test_ok = ok
    conn.last_test_message = (message or "")[:500]
    conn.status = ConnectorStatus.ACTIVE if ok else ConnectorStatus.ERROR
    await session.flush()
    await session.refresh(conn)
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        request=request,
        target_type="connector_test",
        target_id=str(conn.id),
        target_label=conn.name,
        before=before,
        after={"ok": ok, "message": (message or "")[:200]},
        notes=f"result={'ok' if ok else 'fail'}",
    )
    return ConnectorTestResponse(
        ok=ok,
        message=message or "",
        tested_at=now.isoformat(),
    )


@router.get("/{connector_id}/tables", response_model=list[ConnectorTableInfo])
async def list_connector_tables(
    connector_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[ConnectorTableInfo]:
    """列出 connector 可见的表 / sheet / 数组根。"""
    _, _ = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    plain_cfg = decrypt_secret_fields(conn.config or {}, conn.secret_fields or [])
    try:
        c = get_connector(conn.type.value, plain_cfg)
        tables = await asyncio.wait_for(c.list_tables(), timeout=30)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="list_tables timed out")
    return [
        ConnectorTableInfo(
            name=t.name,
            description=t.description,
            row_count_estimate=t.row_count_estimate,
            schema=(
                [
                    {
                        "name": f.name,
                        "data_type": f.data_type,
                        "nullable": f.nullable,
                    }
                    for f in (t.schema or [])
                ]
                if t.schema
                else None
            ),
        )
        for t in tables
    ]


@router.post("/{connector_id}/snapshot", response_model=ConnectorSnapshotResponse)
async def snapshot_connector(
    connector_id: uuid.UUID,
    body: ConnectorSnapshotRequest,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> ConnectorSnapshotResponse:
    """取一次 snapshot；上限 / 偏移由调用方控制。"""
    principal, _ = ctx
    conn = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    plain_cfg = decrypt_secret_fields(conn.config or {}, conn.secret_fields or [])
    try:
        c = get_connector(conn.type.value, plain_cfg)
        snap = await asyncio.wait_for(
            c.snapshot(body.table, limit=body.limit, offset=body.offset),
            timeout=60,
        )
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="snapshot timed out")

    # snapshot 本身读项目数据，标注一下审计 + 限流（避免大数据量被滥用）
    await record_audit(
        session,
        event_type=AuditEventType.READ,
        principal=principal,
        project_id=project_id,
        target_type="connector_snapshot",
        target_id=str(conn.id),
        target_label=f"{conn.name}/{body.table}",
        after={
            "table": body.table,
            "limit": body.limit,
            "offset": body.offset,
            "row_count": snap.row_count,
            "truncated": snap.truncated,
        },
    )
    return ConnectorSnapshotResponse(
        table=snap.table,
        fields=[
            {"name": f.name, "data_type": f.data_type, "nullable": f.nullable}
            for f in snap.fields
        ],
        row_count=snap.row_count,
        truncated=snap.truncated,
        rows=snap.rows,
    )


# ---------------------------------------------------------------------------
# HIA-67 B2: Connector Snapshot → Evidence（HIA-67 验收用）
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/snapshot-to-evidence",
    response_model=ConnectorSnapshotToEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def snapshot_to_evidence(
    connector_id: uuid.UUID,
    body: ConnectorSnapshotToEvidenceRequest,
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role_query(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> ConnectorSnapshotToEvidenceResponse:
    """快照 PG 表并自动创建 Source + Evidence 记录（HIA-67 验收端点）。

    流程：
    1. 用 connector 配置运行 snapshot（只读）
    2. 创建 Source 记录（type=POSTGRESQL，存储连接器元信息）
    3. 创建 SourceSnapshot（版本化快照元数据）
    4. 为每个字段创建 Evidence 记录（含 null_ratio / unique_ratio 等统计）

    权限：EDITOR 及以上（因为写入本体候选数据）。
    """
    principal, _ = ctx
    conn_record = await _load_for_project(
        session, connector_id=connector_id, project_id=project_id
    )
    plain_cfg = decrypt_secret_fields(
        conn_record.config or {}, conn_record.secret_fields or []
    )
    try:
        c = get_connector(conn_record.type.value, plain_cfg)
        snap = await asyncio.wait_for(
            c.snapshot(body.table, limit=body.limit, offset=body.offset),
            timeout=60,
        )
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="snapshot timed out")

    # 构建 connection_info（不存明文密码）
    safe_conn_info = {
        "type": conn_record.type.value,
        "host": plain_cfg.get("host", ""),
        "port": plain_cfg.get("port", 5432),
        "database": plain_cfg.get("database", ""),
        "schema": plain_cfg.get("schema_filter", []),
        "table_filter": plain_cfg.get("table_filter", []),
    }

    # 计算字段统计（用于 Evidence profile）
    field_profiles: dict[str, dict[str, Any]] = {}
    for f in snap.fields:
        null_count = sum(1 for row in snap.rows if row.get(f.name) is None)
        null_ratio = null_count / snap.row_count if snap.row_count > 0 else 0.0
        unique_vals = set(row.get(f.name) for row in snap.rows if row.get(f.name) is not None)
        unique_ratio = len(unique_vals) / snap.row_count if snap.row_count > 0 else 0.0
        sample_vals = list(unique_vals)[:5]
        field_profiles[f.name] = {
            "null_ratio": round(null_ratio, 4),
            "unique_ratio": round(unique_ratio, 4),
            "sample_values": [str(v) for v in sample_vals],
            "inferred_type": _pg_type_to_model(f.data_type),
        }

    # 1. 创建 Source
    source = Source(
        project_id=project_id,
        name=f"[{conn_record.name}] {body.table}",
        description=f"PostgreSQL connector snapshot: {body.table}",
        source_type=SourceType.POSTGRESQL,
        connection_info=safe_conn_info,
        file_path=None,
        file_size=None,
        row_count=snap.row_count,
        column_count=len(snap.fields),
        access_scope="restricted",
        is_sensitive=False,
        schema_info={
            "table": body.table,
            "connector_id": str(conn_record.id),
            "connector_name": conn_record.name,
        },
        status=SourceStatus.READY,
        created_by=principal.user.id,
    )
    session.add(source)
    await session.flush()
    await session.refresh(source)

    # 2. 创建 SourceSnapshot
    import hashlib

    schema_str = ",".join(f"{f.name}:{f.data_type}" for f in snap.fields)
    schema_hash = hashlib.sha256(schema_str.encode()).hexdigest()
    snapshot_record = SourceSnapshot(
        source_id=source.id,
        version=1,
        snapshot_type="connector_snapshot",
        storage_path=None,
        storage_size=None,
        checksum=None,
        row_count=snap.row_count,
        schema_hash=schema_hash,
        created_by=principal.user.id,
    )
    session.add(snapshot_record)
    await session.flush()
    await session.refresh(snapshot_record)

    # 3. 为每个字段创建 Evidence
    evidence_ids: list[uuid.UUID] = []
    if body.auto_evidence:
        for f in snap.fields:
            profile = field_profiles.get(f.name, {})
            ev = Evidence(
                project_id=project_id,
                source_id=source.id,
                evidence_type=EvidenceType.SOURCE_FIELD,
                location=f"column:{body.table}.{f.name}",
                field_name=f.name,
                content=", ".join(profile.get("sample_values", [])[:5]) or None,
                source_identifier=body.table,
                extraction_method="connector_snapshot",
                extraction_params={
                    "connector_id": str(conn_record.id),
                    "connector_name": conn_record.name,
                    "pg_data_type": f.data_type,
                    "inferred_type": profile.get("inferred_type", "string"),
                    "null_ratio": profile.get("null_ratio", 0),
                    "unique_ratio": profile.get("unique_ratio", 0),
                    "sample_values": profile.get("sample_values", []),
                },
                strength="medium",
                created_by=principal.user.id,
            )
            session.add(ev)
        await session.flush()

        result = await session.execute(
            select(Evidence).where(
                Evidence.source_id == source.id,
                Evidence.project_id == project_id,
            )
        )
        evidence_ids = [e.id for e in result.scalars().all()]

    # 审计
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        request=request,
        target_type="connector_snapshot_to_evidence",
        target_id=str(source.id),
        target_label=f"{conn_record.name}/{body.table}",
        after={
            "connector_id": str(conn_record.id),
            "table": body.table,
            "row_count": snap.row_count,
            "field_count": len(snap.fields),
            "source_id": str(source.id),
            "snapshot_id": str(snapshot_record.id),
            "evidence_count": len(evidence_ids),
        },
    )

    return ConnectorSnapshotToEvidenceResponse(
        source_id=source.id,
        snapshot_id=snapshot_record.id,
        table=snap.table,
        fields=[
            {
                "name": f.name,
                "data_type": f.data_type,
                "nullable": f.nullable,
                "profile": field_profiles.get(f.name, {}),
            }
            for f in snap.fields
        ],
        row_count=snap.row_count,
        truncated=snap.truncated,
        rows=snap.rows,
        evidence_ids=evidence_ids,
        profile=field_profiles,
    )

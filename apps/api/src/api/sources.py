"""数据源 API 路由"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.evidence import (
    Source,
    SourceSnapshot,
    Evidence,
    ProfilingRun,
    SourceType,
    SourceStatus,
    EvidenceType,
)

router = APIRouter(prefix="/sources", tags=["数据源"])


# ============ Pydantic 模型 ============

class SourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    source_type: SourceType
    connection_info: Optional[dict] = None
    access_scope: str = "restricted"


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[SourceStatus] = None


class SourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    source_type: SourceType
    status: SourceStatus
    file_size: Optional[int]
    row_count: Optional[int]
    column_count: Optional[int]
    created_at: str

    model_config = {"from_attributes": True}


class ProfilingRunResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: str
    total_rows: Optional[int]
    sampled_rows: Optional[int]
    total_columns: Optional[int]
    error_message: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    created_at: str

    model_config = {"from_attributes": True}


class EvidenceCreate(BaseModel):
    evidence_type: EvidenceType
    location: Optional[str] = None
    field_name: Optional[str] = None
    content: Optional[str] = None
    source_identifier: Optional[str] = None
    ontology_class_iri: Optional[str] = None
    property_iri: Optional[str] = None
    strength: str = "medium"
    notes: Optional[str] = None


class EvidenceResponse(BaseModel):
    id: uuid.UUID
    evidence_type: EvidenceType
    location: Optional[str]
    field_name: Optional[str]
    content: Optional[str]
    source_identifier: Optional[str]
    ontology_class_iri: Optional[str]
    property_iri: Optional[str]
    is_confirmed: bool
    strength: str
    created_at: str

    model_config = {"from_attributes": True}


# ============ 数据源路由 ============

@router.get("", response_model=list[SourceResponse])
async def list_sources(
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    source_type: Optional[SourceType] = Query(None),
    status: Optional[SourceStatus] = Query(None),
) -> list[SourceResponse]:
    query = select(Source).where(Source.project_id == project_id)
    
    if source_type:
        query = query.where(Source.source_type == source_type)
    if status:
        query = query.where(Source.status == status)
    
    query = query.order_by(Source.created_at.desc())
    
    result = await session.execute(query)
    sources = result.scalars().all()
    
    return [
        SourceResponse(
            id=s.id,
            name=s.name,
            description=s.description,
            source_type=s.source_type,
            status=s.status,
            file_size=s.file_size,
            row_count=s.row_count,
            column_count=s.column_count,
            created_at=s.created_at.isoformat() if s.created_at else "",
        )
        for s in sources
    ]


@router.post("", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    data: SourceCreate,
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
) -> SourceResponse:
    source = Source(
        project_id=project_id,
        name=data.name,
        description=data.description,
        source_type=data.source_type,
        connection_info=data.connection_info,
        access_scope=data.access_scope,
        status=SourceStatus.UPLOADED,
    )
    
    session.add(source)
    await session.flush()
    await session.refresh(source)
    
    return SourceResponse(
        id=source.id,
        name=source.name,
        description=source.description,
        source_type=source.source_type,
        status=source.status,
        file_size=source.file_size,
        row_count=source.row_count,
        column_count=source.column_count,
        created_at=source.created_at.isoformat() if source.created_at else "",
    )


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    
    return SourceResponse(
        id=source.id,
        name=source.name,
        description=source.description,
        source_type=source.source_type,
        status=source.status,
        file_size=source.file_size,
        row_count=source.row_count,
        column_count=source.column_count,
        created_at=source.created_at.isoformat() if source.created_at else "",
    )


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: uuid.UUID,
    data: SourceUpdate,
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(source, field, value)
    
    await session.flush()
    await session.refresh(source)
    
    return SourceResponse(
        id=source.id,
        name=source.name,
        description=source.description,
        source_type=source.source_type,
        status=source.status,
        file_size=source.file_size,
        row_count=source.row_count,
        column_count=source.column_count,
        created_at=source.created_at.isoformat() if source.created_at else "",
    )


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    
    await session.delete(source)


# ============ 证据路由 ============

@router.get("/{source_id}/evidences", response_model=list[EvidenceResponse])
async def list_evidences(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    ontology_class_iri: Optional[str] = Query(None),
    property_iri: Optional[str] = Query(None),
    is_confirmed: Optional[bool] = Query(None),
) -> list[EvidenceResponse]:
    query = select(Evidence).where(Evidence.source_id == source_id)
    
    if ontology_class_iri:
        query = query.where(Evidence.ontology_class_iri == ontology_class_iri)
    if property_iri:
        query = query.where(Evidence.property_iri == property_iri)
    if is_confirmed is not None:
        query = query.where(Evidence.is_confirmed == is_confirmed)
    
    query = query.order_by(Evidence.created_at.desc())
    
    result = await session.execute(query)
    evidences = result.scalars().all()
    
    return [
        EvidenceResponse(
            id=e.id,
            evidence_type=e.evidence_type,
            location=e.location,
            field_name=e.field_name,
            content=e.content,
            source_identifier=e.source_identifier,
            ontology_class_iri=e.ontology_class_iri,
            property_iri=e.property_iri,
            is_confirmed=e.is_confirmed,
            strength=e.strength,
            created_at=e.created_at.isoformat() if e.created_at else "",
        )
        for e in evidences
    ]


@router.post("/{source_id}/evidences", response_model=EvidenceResponse, status_code=status.HTTP_201_CREATED)
async def create_evidence(
    source_id: uuid.UUID,
    data: EvidenceCreate,
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
) -> EvidenceResponse:
    # 获取数据源的项目 ID
    source_result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = source_result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    
    evidence = Evidence(
        project_id=project_id,
        source_id=source_id,
        evidence_type=data.evidence_type,
        location=data.location,
        field_name=data.field_name,
        content=data.content,
        source_identifier=data.source_identifier,
        ontology_class_iri=data.ontology_class_iri,
        property_iri=data.property_iri,
        strength=data.strength,
        notes=data.notes,
    )
    
    session.add(evidence)
    await session.flush()
    await session.refresh(evidence)
    
    return EvidenceResponse(
        id=evidence.id,
        evidence_type=evidence.evidence_type,
        location=evidence.location,
        field_name=evidence.field_name,
        content=evidence.content,
        source_identifier=evidence.source_identifier,
        ontology_class_iri=evidence.ontology_class_iri,
        property_iri=evidence.property_iri,
        is_confirmed=evidence.is_confirmed,
        strength=evidence.strength,
        created_at=evidence.created_at.isoformat() if evidence.created_at else "",
    )


# ============ 剖析路由 ============

@router.get("/{source_id}/profiling", response_model=list[ProfilingRunResponse])
async def list_profiling_runs(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ProfilingRunResponse]:
    result = await session.execute(
        select(ProfilingRun)
        .where(ProfilingRun.source_id == source_id)
        .order_by(ProfilingRun.created_at.desc())
    )
    runs = result.scalars().all()
    return [
        ProfilingRunResponse(
            id=r.id,
            source_id=r.source_id,
            status=r.status.value if hasattr(r.status, 'value') else str(r.status),
            total_rows=r.total_rows,
            sampled_rows=r.sampled_rows,
            total_columns=r.total_columns,
            error_message=r.error_message,
            started_at=r.started_at.isoformat() if r.started_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            duration_ms=r.duration_ms,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in runs
    ]


@router.post("/{source_id}/profiling", status_code=status.HTTP_201_CREATED)
async def create_profiling_run(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """创建剖析运行"""
    profiling = ProfilingRun(
        source_id=source_id,
        status="pending",
    )

    session.add(profiling)
    await session.flush()
    await session.refresh(profiling)

    return {
        "id": str(profiling.id),
        "status": profiling.status,
        "message": "剖析任务已创建",
    }


# =====================================================================
# Evidence 项目级收件箱路由（独立 prefix，避免 source 嵌套限制）
# =====================================================================


ev_router = APIRouter(prefix="/evidences", tags=["证据"])


class EvidenceAlignInput(BaseModel):
    """字段对齐输入"""

    ontology_class_iri: str = Field(..., max_length=500)
    property_iri: Optional[str] = Field(None, max_length=500)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None


class EvidenceAlignResponse(BaseModel):
    """对齐输出"""

    id: uuid.UUID
    ontology_class_iri: str
    property_iri: Optional[str]
    alignment_confirmed: bool
    created_at: str


class EvidenceInboxResponse(BaseModel):
    """项目证据收件箱条目"""

    id: uuid.UUID
    source_id: Optional[uuid.UUID]
    source_name: Optional[str]
    evidence_type: EvidenceType
    field_name: Optional[str]
    content: Optional[str]
    ontology_class_iri: Optional[str]
    property_iri: Optional[str]
    is_confirmed: bool
    strength: str
    created_at: str

    model_config = {"from_attributes": True}


class ProfilingExecuteRequest(BaseModel):
    """执行剖析请求"""

    sample_size: int = Field(default=1000, ge=100, le=50000)
    include_stats: bool = True
    detect_enums: bool = True


class FieldProfileResponse(BaseModel):
    """字段剖析结果"""

    field_name: str
    data_type: str
    total_count: int
    null_count: int
    null_ratio: float
    unique_count: int
    unique_ratio: float
    sample_values: list[str]
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    avg_length: Optional[float] = None
    detected_enum_values: Optional[list[str]] = None
    suggested_ontology_type: Optional[str] = None
    suggested_property_type: Optional[str] = None


@ev_router.get("/project/{project_id}", response_model=list[EvidenceInboxResponse])
async def evidence_inbox(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    source_id: Optional[uuid.UUID] = Query(None),
    ontology_class_iri: Optional[str] = Query(None),
    is_confirmed: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> list[EvidenceInboxResponse]:
    """项目级证据收件箱（合并所有来源的证据）"""
    query = select(Evidence, Source.name).outerjoin(
        Source, Evidence.source_id == Source.id
    ).where(Evidence.project_id == project_id)

    if source_id:
        query = query.where(Evidence.source_id == source_id)
    if ontology_class_iri:
        query = query.where(Evidence.ontology_class_iri == ontology_class_iri)
    if is_confirmed is not None:
        query = query.where(Evidence.is_confirmed == is_confirmed)
    if search:
        query = query.where(Evidence.content.ilike(f"%{search}%"))

    query = query.order_by(Evidence.created_at.desc())
    query = query.offset((page - 1) * size).limit(size)

    result = await session.execute(query)
    rows = result.all()

    return [
        EvidenceInboxResponse(
            id=e.id,
            source_id=e.source_id,
            source_name=source_name,
            evidence_type=e.evidence_type,
            field_name=e.field_name,
            content=e.content,
            ontology_class_iri=e.ontology_class_iri,
            property_iri=e.property_iri,
            is_confirmed=e.is_confirmed,
            strength=e.strength,
            created_at=e.created_at.isoformat() if e.created_at else "",
        )
        for e, source_name in rows
    ]


@ev_router.get("/{evidence_id}", response_model=EvidenceResponse)
async def get_evidence(
    evidence_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EvidenceResponse:
    """获取证据详情"""
    result = await session.execute(
        select(Evidence).where(Evidence.id == evidence_id)
    )
    e = result.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="证据不存在")

    return EvidenceResponse(
        id=e.id,
        evidence_type=e.evidence_type,
        location=e.location,
        field_name=e.field_name,
        content=e.content,
        source_identifier=e.source_identifier,
        ontology_class_iri=e.ontology_class_iri,
        property_iri=e.property_iri,
        is_confirmed=e.is_confirmed,
        strength=e.strength,
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


@ev_router.patch("/{evidence_id}/align", response_model=EvidenceAlignResponse)
async def align_evidence(
    evidence_id: uuid.UUID,
    data: EvidenceAlignInput,
    session: AsyncSession = Depends(get_session),
) -> EvidenceAlignResponse:
    """将证据对齐到本体类/属性

    建立 field → ontology class + property 的语义映射。
    多次对齐会覆盖前一次。
    """
    result = await session.execute(
        select(Evidence).where(Evidence.id == evidence_id)
    )
    e = result.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="证据不存在")

    e.ontology_class_iri = data.ontology_class_iri
    e.property_iri = data.property_iri
    e.is_confirmed = True

    await session.flush()
    await session.refresh(e)

    return EvidenceAlignResponse(
        id=e.id,
        ontology_class_iri=e.ontology_class_iri,
        property_iri=e.property_iri,
        alignment_confirmed=e.is_confirmed,
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


@ev_router.patch("/{evidence_id}/reject", response_model=EvidenceResponse)
async def reject_evidence(
    evidence_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EvidenceResponse:
    """拒绝证据（标记为未确认）"""
    result = await session.execute(
        select(Evidence).where(Evidence.id == evidence_id)
    )
    e = result.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="证据不存在")

    e.is_confirmed = False
    e.ontology_class_iri = None
    e.property_iri = None

    await session.flush()
    await session.refresh(e)

    return EvidenceResponse(
        id=e.id,
        evidence_type=e.evidence_type,
        location=e.location,
        field_name=e.field_name,
        content=e.content,
        source_identifier=e.source_identifier,
        ontology_class_iri=e.ontology_class_iri,
        property_iri=e.property_iri,
        is_confirmed=e.is_confirmed,
        strength=e.strength,
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


@ev_router.post("/bulk-confirm", status_code=status.HTTP_200_OK)
async def bulk_confirm_evidences(
    evidence_ids: list[uuid.UUID],
    ontology_class_iri: str = Query(..., max_length=500),
    property_iri: Optional[str] = Query(None, max_length=500),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """批量确认证据并对齐到本体"""
    if not evidence_ids:
        return {"updated": 0}

    result = await session.execute(
        select(Evidence).where(Evidence.id.in_(evidence_ids))
    )
    evidences = result.scalars().all()

    updated = 0
    for e in evidences:
        e.ontology_class_iri = ontology_class_iri
        e.property_iri = property_iri
        e.is_confirmed = True
        updated += 1

    await session.flush()
    return {"updated": updated, "total": len(evidence_ids)}


@ev_router.post("/bulk-reject", status_code=status.HTTP_200_OK)
async def bulk_reject_evidences(
    evidence_ids: list[uuid.UUID],
    session: AsyncSession = Depends(get_session),
) -> dict:
    """批量拒绝证据"""
    if not evidence_ids:
        return {"updated": 0}

    result = await session.execute(
        select(Evidence).where(Evidence.id.in_(evidence_ids))
    )
    evidences = result.scalars().all()

    updated = 0
    for e in evidences:
        e.is_confirmed = False
        e.ontology_class_iri = None
        e.property_iri = None
        updated += 1

    await session.flush()
    return {"updated": updated, "total": len(evidence_ids)}


# =====================================================================
# 字段剖析执行路由（独立 prefix）
# =====================================================================


prof_router = APIRouter(prefix="/profiling", tags=["剖析"])


@prof_router.post("/sources/{source_id}/execute", response_model=dict)
async def execute_field_profiling(
    source_id: uuid.UUID,
    data: ProfilingExecuteRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """执行字段剖析

    基于源数据的抽样样本，推断字段的：
    - 数据类型（string / integer / float / boolean / date / enum）
    - 空值比例
    - 枚举值（detect_enums=True 时）
    - 建议的本体属性类型

    返回字段统计摘要，不做持久化（结果通过 profiling run 存储）。
    """
    # 确认源存在
    src_result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = src_result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")

    # 获取已存储的 schema_info
    schema = source.schema_info or {}
    fields = schema.get("fields", [])

    if not fields:
        # 尝试从 connection_info 读取（PostgreSQL 等结构化源）
        conn = source.connection_info or {}
        if source.source_type == "postgresql" and "query" in conn:
            # 简化实现：返回占位结果，实际应连接 DB 执行
            return {
                "source_id": str(source_id),
                "status": "pending_implementation",
                "message": "PostgreSQL 剖析需要数据库连接，参见连接器插件",
                "fields": [],
            }
        return {
            "source_id": str(source_id),
            "status": "no_schema",
            "message": "源缺少 schema_info，请先上传或解析数据",
            "fields": [],
        }

    # 对每个字段生成统计（基于 schema 推断类型 + 枚举）
    profiles: list[dict] = []
    for field in fields[:50]:  # 最多50个字段
        f_name = field.get("name", "")
        f_type = field.get("type", "string").lower()

        profile = {
            "field_name": f_name,
            "data_type": f_type,
            "total_count": data.sample_size,
            "null_count": field.get("null_count", 0),
            "null_ratio": field.get("null_ratio", 0.0),
            "unique_count": field.get("unique_count", 0),
            "unique_ratio": field.get("unique_ratio", 0.0),
            "sample_values": field.get("sample_values", [])[:10],
            "min_value": field.get("min_value"),
            "max_value": field.get("max_value"),
            "avg_length": field.get("avg_length"),
            "detected_enum_values": None,
            "suggested_ontology_type": _infer_ontology_type(f_type, field),
            "suggested_property_type": _infer_property_type(f_type),
        }

        # 检测枚举值
        if data.detect_enums:
            enum_vals = field.get("enum_values", [])
            if enum_vals:
                profile["detected_enum_values"] = enum_vals[:50]
            elif profile["unique_ratio"] < 0.05 and profile["unique_count"] <= 50:
                # 唯一值太少，视为枚举候选
                profile["detected_enum_values"] = field.get("sample_values", [])[:50]

        profiles.append(profile)

    # 更新 profiling run 状态
    run_result = await session.execute(
        select(ProfilingRun)
        .where(ProfilingRun.source_id == source_id)
        .order_by(ProfilingRun.created_at.desc())
        .limit(1)
    )
    run = run_result.scalar_one_or_none()
    if run:
        run.status = "completed"
        run.results = {"fields": profiles}
        run.total_rows = data.sample_size
        run.total_columns = len(profiles)
        await session.flush()

    return {
        "source_id": str(source_id),
        "run_id": str(run.id) if run else None,
        "status": "completed",
        "field_count": len(profiles),
        "fields": profiles,
    }


def _infer_ontology_type(field_type: str, field: dict) -> str:
    """推断建议的本体类IRI（根据数据特征）"""
    t = field_type.lower()
    if t in ("integer", "bigint", "smallint"):
        return "xsd:integer"
    if t in ("numeric", "decimal", "float", "double"):
        return "xsd:decimal"
    if t == "boolean":
        return "xsd:boolean"
    if t in ("date", "datetime", "timestamp"):
        return "xsd:dateTime"
    if t == "json":
        return "xsd:string"
    return "xsd:string"


def _infer_property_type(field_type: str) -> str:
    """推断建议的属性类型"""
    t = field_type.lower()
    if t in ("integer", "bigint", "smallint", "numeric", "decimal", "float", "double"):
        return "datatype"
    if t == "boolean":
        return "datatype"
    if t in ("date", "datetime", "timestamp"):
        return "datatype"
    if t in ("json", "text", "string"):
        return "datatype"
    return "datatype"


# =====================================================================
# 将子路由注册到主应用（通过 include_router）
# sources.py 被 main.py 导入后，这部分通过 main.py 注册
# =====================================================================
# 注意：ev_router 和 prof_router 需在 main.py 中注册
# 见 src/api/main.py


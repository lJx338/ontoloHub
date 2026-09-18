"""本体 API 路由

阶段二改动（与 src/db/ontology.py 阶段一重构对齐）：
- OntologyCreate/Response 用 kind 枚举替代 is_base/is_industry/industry_type/current_version
- OntologyClassResponse 补 alignment 字段
- 新增 Constraint CRUD 端点

阶段三改动（发布 / 导出 / Catalog）：
- POST /ontologies/{id}/publish：快照 + 状态变更为 published
- POST /ontologies/{id}/export：生成 OWL/TTL/JSON-LD
- 新增 /catalog 子路由（参考本体管理）
"""
from __future__ import annotations

import uuid
import re
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.ontology import (
    Ontology,
    OntologyKind,
    OntologyStatus,
    OntologyClass,
    Property,
    Constraint,
    Relation,
    OntologyVersion,
    OntologyVersionStatus,
    ConstraintType,
    SeverityLevel,
    ClassType,
    PropertyType,
    RelationType,
)

router = APIRouter(prefix="/ontologies", tags=["本体"])


# =====================================================================
# Pydantic: 本体
# =====================================================================


class OntologyCreate(BaseModel):
    """创建本体"""

    name: str = Field(..., min_length=1, max_length=255)
    namespace: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    project_id: Optional[uuid.UUID] = None
    kind: OntologyKind = OntologyKind.PROJECT
    # 参考本体字段（可选）
    standard_name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = Field(None, max_length=500)
    source_format: Optional[str] = Field(None, max_length=20)
    version: Optional[str] = Field(None, max_length=50)


class OntologyUpdate(BaseModel):
    """更新本体（kind / namespace 不可改）"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    standard_name: Optional[str] = None
    source_url: Optional[str] = None
    source_format: Optional[str] = None


class OntologyStatusUpdate(BaseModel):
    """修改本体状态"""

    status: OntologyStatus


class OntologyResponse(BaseModel):
    """本体响应"""

    id: uuid.UUID
    name: str
    namespace: str
    description: Optional[str]
    project_id: Optional[uuid.UUID]
    kind: OntologyKind
    status: OntologyStatus
    version: Optional[str]
    standard_name: Optional[str]
    source_url: Optional[str]
    source_format: Optional[str]
    class_count: int
    property_count: int
    created_at: str

    model_config = {"from_attributes": True}


class OntologyListResponse(BaseModel):
    """列表响应（轻量）"""

    id: uuid.UUID
    name: str
    namespace: str
    kind: OntologyKind
    status: OntologyStatus
    version: Optional[str]
    class_count: int
    property_count: int
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 类
# =====================================================================


class AlignmentInput(BaseModel):
    """对齐输入"""

    reference_class_id: uuid.UUID
    notes: Optional[str] = None


class AlignmentResponse(BaseModel):
    """对齐输出"""

    reference_class_id: uuid.UUID
    notes: Optional[str] = None


class OntologyClassCreate(BaseModel):
    """创建类"""

    name: str = Field(..., min_length=1, max_length=255)
    local_name: Optional[str] = Field(None, max_length=255)
    iri: str = Field(..., min_length=1, max_length=500)
    class_type: ClassType = ClassType.ONTOLOGY_CLASS
    parent_iri: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    examples: Optional[list[str]] = None
    enum_values: Optional[list[str]] = None
    alignment: Optional[AlignmentInput] = None


class OntologyClassUpdate(BaseModel):
    """更新类"""

    name: Optional[str] = None
    local_name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    examples: Optional[list[str]] = None
    alignment: Optional[AlignmentInput] = None


class OntologyClassResponse(BaseModel):
    """类响应"""

    id: uuid.UUID
    name: str
    local_name: Optional[str]
    iri: str
    class_type: ClassType
    parent_iri: Optional[str]
    level: int
    description: Optional[str]
    definition: Optional[str]
    is_locked: bool
    alignment: Optional[AlignmentResponse] = None
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 属性
# =====================================================================


class PropertyCreate(BaseModel):
    """创建属性"""

    name: str = Field(..., min_length=1, max_length=255)
    local_name: Optional[str] = Field(None, max_length=255)
    iri: str = Field(..., min_length=1, max_length=500)
    property_type: PropertyType = PropertyType.DATATYPE_PROPERTY
    domain_iri: Optional[str] = None
    range_type: Optional[str] = None
    range_class_iri: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    is_required: bool = False
    is_multivalued: bool = False


class PropertyUpdate(BaseModel):
    """更新属性"""

    name: Optional[str] = None
    local_name: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None


class PropertyResponse(BaseModel):
    """属性响应"""

    id: uuid.UUID
    name: str
    local_name: Optional[str]
    iri: str
    property_type: PropertyType
    domain_iri: Optional[str]
    range_type: Optional[str]
    range_class_iri: Optional[str]
    description: Optional[str]
    unit: Optional[str]
    is_required: bool
    is_multivalued: bool
    is_locked: bool
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 约束
# =====================================================================


class ConstraintCreate(BaseModel):
    """创建约束"""

    name: Optional[str] = Field(None, max_length=255)
    ontology_class_id: Optional[uuid.UUID] = None
    property_id: Optional[uuid.UUID] = None
    target_class_iri: Optional[str] = None
    property_iri: Optional[str] = None
    constraint_type: ConstraintType
    severity: SeverityLevel = SeverityLevel.WARNING
    value: Optional[dict] = None
    description: Optional[str] = None


class ConstraintUpdate(BaseModel):
    """更新约束"""

    name: Optional[str] = None
    severity: Optional[SeverityLevel] = None
    value: Optional[dict] = None
    description: Optional[str] = None


class ConstraintResponse(BaseModel):
    """约束响应"""

    id: uuid.UUID
    name: Optional[str]
    ontology_class_id: Optional[uuid.UUID]
    property_id: Optional[uuid.UUID]
    target_class_iri: Optional[str]
    property_iri: Optional[str]
    constraint_type: ConstraintType
    severity: SeverityLevel
    value: Optional[dict]
    description: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 关系
# =====================================================================


class RelationCreate(BaseModel):
    """创建关系"""

    name: str = Field(..., min_length=1, max_length=255)
    local_name: Optional[str] = Field(None, max_length=255)
    iri: str = Field(..., min_length=1, max_length=500)
    relation_type: RelationType = RelationType.OBJECT
    source_class_iri: Optional[str] = None
    target_class_iri: Optional[str] = None
    is_required: bool = False
    is_transitive: bool = False
    is_symmetric: bool = False
    is_inverse_functional: bool = False
    description: Optional[str] = None


class RelationResponse(BaseModel):
    """关系响应"""

    id: uuid.UUID
    name: str
    local_name: Optional[str]
    iri: str
    relation_type: RelationType
    source_class_iri: Optional[str]
    target_class_iri: Optional[str]
    is_required: bool
    is_transitive: bool
    is_symmetric: bool
    is_inverse_functional: bool
    description: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# 辅助
# =====================================================================


def _to_iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# =====================================================================
# 本体路由
# =====================================================================


@router.get("", response_model=list[OntologyListResponse])
async def list_ontologies(
    session: AsyncSession = Depends(get_session),
    project_id: Optional[uuid.UUID] = Query(None),
    kind: Optional[OntologyKind] = Query(None),
    status: Optional[OntologyStatus] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> list[OntologyListResponse]:
    """列出本体"""
    query = select(Ontology)

    if project_id:
        query = query.where(Ontology.project_id == project_id)
    if kind is not None:
        query = query.where(Ontology.kind == kind)
    if status is not None:
        query = query.where(Ontology.status == status)
    if search:
        query = query.where(Ontology.name.ilike(f"%{search}%"))

    query = query.order_by(Ontology.created_at.desc())
    query = query.offset((page - 1) * size).limit(size)

    result = await session.execute(query)
    ontologies = result.scalars().all()

    return [
        OntologyListResponse(
            id=o.id,
            name=o.name,
            namespace=o.namespace,
            kind=o.kind,
            status=o.status,
            version=o.version,
            class_count=o.class_count,
            property_count=o.property_count,
            created_at=_to_iso(o.created_at),
        )
        for o in ontologies
    ]


@router.post("", response_model=OntologyResponse, status_code=status.HTTP_201_CREATED)
async def create_ontology(
    data: OntologyCreate,
    session: AsyncSession = Depends(get_session),
) -> OntologyResponse:
    """创建本体"""
    ontology = Ontology(
        name=data.name,
        namespace=data.namespace,
        description=data.description,
        project_id=data.project_id,
        kind=data.kind,
        standard_name=data.standard_name,
        source_url=data.source_url,
        source_format=data.source_format,
        version=data.version,
        status=OntologyStatus.DRAFT,
    )

    session.add(ontology)
    await session.flush()
    await session.refresh(ontology)

    return OntologyResponse(
        id=ontology.id,
        name=ontology.name,
        namespace=ontology.namespace,
        description=ontology.description,
        project_id=ontology.project_id,
        kind=ontology.kind,
        status=ontology.status,
        version=ontology.version,
        standard_name=ontology.standard_name,
        source_url=ontology.source_url,
        source_format=ontology.source_format,
        class_count=ontology.class_count,
        property_count=ontology.property_count,
        created_at=_to_iso(ontology.created_at),
    )


@router.get("/{ontology_id}", response_model=OntologyResponse)
async def get_ontology(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> OntologyResponse:
    """获取本体详情"""
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()

    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    return OntologyResponse(
        id=ontology.id,
        name=ontology.name,
        namespace=ontology.namespace,
        description=ontology.description,
        project_id=ontology.project_id,
        kind=ontology.kind,
        status=ontology.status,
        version=ontology.version,
        standard_name=ontology.standard_name,
        source_url=ontology.source_url,
        source_format=ontology.source_format,
        class_count=ontology.class_count,
        property_count=ontology.property_count,
        created_at=_to_iso(ontology.created_at),
    )


@router.patch("/{ontology_id}", response_model=OntologyResponse)
async def update_ontology(
    ontology_id: uuid.UUID,
    data: OntologyUpdate,
    session: AsyncSession = Depends(get_session),
) -> OntologyResponse:
    """修改本体（kind / namespace 不可改）"""
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()

    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    if ontology.status == OntologyStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="已发布的本体不可修改")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(ontology, field, value)

    await session.flush()
    await session.refresh(ontology)

    return OntologyResponse(
        id=ontology.id,
        name=ontology.name,
        namespace=ontology.namespace,
        description=ontology.description,
        project_id=ontology.project_id,
        kind=ontology.kind,
        status=ontology.status,
        version=ontology.version,
        standard_name=ontology.standard_name,
        source_url=ontology.source_url,
        source_format=ontology.source_format,
        class_count=ontology.class_count,
        property_count=ontology.property_count,
        created_at=_to_iso(ontology.created_at),
    )


@router.patch("/{ontology_id}/status", response_model=OntologyResponse)
async def update_ontology_status(
    ontology_id: uuid.UUID,
    data: OntologyStatusUpdate,
    session: AsyncSession = Depends(get_session),
) -> OntologyResponse:
    """修改本体状态"""
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()

    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    ontology.status = data.status
    await session.flush()
    await session.refresh(ontology)

    return OntologyResponse(
        id=ontology.id,
        name=ontology.name,
        namespace=ontology.namespace,
        description=ontology.description,
        project_id=ontology.project_id,
        kind=ontology.kind,
        status=ontology.status,
        version=ontology.version,
        standard_name=ontology.standard_name,
        source_url=ontology.source_url,
        source_format=ontology.source_format,
        class_count=ontology.class_count,
        property_count=ontology.property_count,
        created_at=_to_iso(ontology.created_at),
    )


@router.delete("/{ontology_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ontology(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除本体（仅 draft 可删）"""
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()

    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    if ontology.status != OntologyStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail="仅草稿状态可删除",
        )

    await session.delete(ontology)


# =====================================================================
# 类路由
# =====================================================================


@router.get("/{ontology_id}/classes", response_model=list[OntologyClassResponse])
async def list_classes(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    parent_iri: Optional[str] = Query(None),
    level: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
) -> list[OntologyClassResponse]:
    """列出类"""
    query = select(OntologyClass).where(OntologyClass.ontology_id == ontology_id)

    if parent_iri:
        query = query.where(OntologyClass.parent_iri == parent_iri)
    if level is not None:
        query = query.where(OntologyClass.level == level)
    if search:
        query = query.where(OntologyClass.name.ilike(f"%{search}%"))

    query = query.order_by(OntologyClass.name)

    result = await session.execute(query)
    classes = result.scalars().all()

    return [
        OntologyClassResponse(
            id=c.id,
            name=c.name,
            local_name=c.local_name,
            iri=c.iri,
            class_type=c.class_type,
            parent_iri=c.parent_iri,
            level=c.level,
            description=c.description,
            definition=c.definition,
            is_locked=c.is_locked,
            alignment=AlignmentResponse(**c.alignment)
            if c.alignment
            else None,
            created_at=_to_iso(c.created_at),
        )
        for c in classes
    ]


@router.post("/{ontology_id}/classes", response_model=OntologyClassResponse, status_code=status.HTTP_201_CREATED)
async def create_class(
    ontology_id: uuid.UUID,
    data: OntologyClassCreate,
    session: AsyncSession = Depends(get_session),
) -> OntologyClassResponse:
    """创建类"""
    # 校验本体存在
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    if not onto_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="本体不存在")

    # 父类层级
    level = 0
    if data.parent_iri:
        parent_result = await session.execute(
            select(OntologyClass).where(
                and_(
                    OntologyClass.iri == data.parent_iri,
                    OntologyClass.ontology_id == ontology_id,
                )
            )
        )
        parent = parent_result.scalar_one_or_none()
        if parent:
            level = parent.level + 1

    # IRI 唯一性
    iri_result = await session.execute(
        select(OntologyClass).where(
            and_(
                OntologyClass.iri == data.iri,
                OntologyClass.ontology_id == ontology_id,
            )
        )
    )
    if iri_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="IRI 已存在")

    # alignment 转换
    alignment_dict = None
    if data.alignment:
        alignment_dict = {
            "reference_class_id": str(data.alignment.reference_class_id),
            "notes": data.alignment.notes,
        }

    ontology_class = OntologyClass(
        ontology_id=ontology_id,
        name=data.name,
        local_name=data.local_name,
        iri=data.iri,
        class_type=data.class_type,
        parent_iri=data.parent_iri,
        level=level,
        description=data.description,
        definition=data.definition,
        examples=data.examples,
        enum_values=data.enum_values,
        alignment=alignment_dict,
    )

    session.add(ontology_class)
    await session.flush()
    await session.refresh(ontology_class)

    return OntologyClassResponse(
        id=ontology_class.id,
        name=ontology_class.name,
        local_name=ontology_class.local_name,
        iri=ontology_class.iri,
        class_type=ontology_class.class_type,
        parent_iri=ontology_class.parent_iri,
        level=ontology_class.level,
        description=ontology_class.description,
        definition=ontology_class.definition,
        is_locked=ontology_class.is_locked,
        alignment=AlignmentResponse(**ontology_class.alignment)
        if ontology_class.alignment
        else None,
        created_at=_to_iso(ontology_class.created_at),
    )


@router.get("/{ontology_id}/classes/{class_id}", response_model=OntologyClassResponse)
async def get_class(
    ontology_id: uuid.UUID,
    class_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> OntologyClassResponse:
    """获取类"""
    result = await session.execute(
        select(OntologyClass).where(
            and_(
                OntologyClass.id == class_id,
                OntologyClass.ontology_id == ontology_id,
            )
        )
    )
    ontology_class = result.scalar_one_or_none()

    if not ontology_class:
        raise HTTPException(status_code=404, detail="类不存在")

    return OntologyClassResponse(
        id=ontology_class.id,
        name=ontology_class.name,
        local_name=ontology_class.local_name,
        iri=ontology_class.iri,
        class_type=ontology_class.class_type,
        parent_iri=ontology_class.parent_iri,
        level=ontology_class.level,
        description=ontology_class.description,
        definition=ontology_class.definition,
        is_locked=ontology_class.is_locked,
        alignment=AlignmentResponse(**ontology_class.alignment)
        if ontology_class.alignment
        else None,
        created_at=_to_iso(ontology_class.created_at),
    )


@router.patch("/{ontology_id}/classes/{class_id}", response_model=OntologyClassResponse)
async def update_class(
    ontology_id: uuid.UUID,
    class_id: uuid.UUID,
    data: OntologyClassUpdate,
    session: AsyncSession = Depends(get_session),
) -> OntologyClassResponse:
    """更新类"""
    result = await session.execute(
        select(OntologyClass).where(
            and_(
                OntologyClass.id == class_id,
                OntologyClass.ontology_id == ontology_id,
            )
        )
    )
    ontology_class = result.scalar_one_or_none()

    if not ontology_class:
        raise HTTPException(status_code=404, detail="类不存在")

    if ontology_class.is_locked:
        raise HTTPException(status_code=400, detail="类已锁定，无法修改")

    update_data = data.model_dump(exclude_unset=True)
    if "alignment" in update_data and update_data["alignment"]:
        alignment_in = update_data["alignment"]
        update_data["alignment"] = {
            "reference_class_id": str(alignment_in["reference_class_id"]),
            "notes": alignment_in.get("notes"),
        }

    for field, value in update_data.items():
        setattr(ontology_class, field, value)

    await session.flush()
    await session.refresh(ontology_class)

    return OntologyClassResponse(
        id=ontology_class.id,
        name=ontology_class.name,
        local_name=ontology_class.local_name,
        iri=ontology_class.iri,
        class_type=ontology_class.class_type,
        parent_iri=ontology_class.parent_iri,
        level=ontology_class.level,
        description=ontology_class.description,
        definition=ontology_class.definition,
        is_locked=ontology_class.is_locked,
        alignment=AlignmentResponse(**ontology_class.alignment)
        if ontology_class.alignment
        else None,
        created_at=_to_iso(ontology_class.created_at),
    )


@router.delete("/{ontology_id}/classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_class(
    ontology_id: uuid.UUID,
    class_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除类"""
    result = await session.execute(
        select(OntologyClass).where(
            and_(
                OntologyClass.id == class_id,
                OntologyClass.ontology_id == ontology_id,
            )
        )
    )
    ontology_class = result.scalar_one_or_none()

    if not ontology_class:
        raise HTTPException(status_code=404, detail="类不存在")

    if ontology_class.is_locked:
        raise HTTPException(status_code=400, detail="类已锁定，无法删除")

    await session.delete(ontology_class)


# =====================================================================
# 属性路由
# =====================================================================


@router.get("/{ontology_id}/properties", response_model=list[PropertyResponse])
async def list_properties(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    property_type: Optional[PropertyType] = Query(None),
    domain_iri: Optional[str] = Query(None),
) -> list[PropertyResponse]:
    """列出属性"""
    query = select(Property).where(Property.ontology_id == ontology_id)

    if property_type is not None:
        query = query.where(Property.property_type == property_type)
    if domain_iri:
        query = query.where(Property.domain_iri == domain_iri)

    query = query.order_by(Property.name)

    result = await session.execute(query)
    properties = result.scalars().all()

    return [
        PropertyResponse(
            id=p.id,
            name=p.name,
            local_name=p.local_name,
            iri=p.iri,
            property_type=p.property_type,
            domain_iri=p.domain_iri,
            range_type=p.range_type,
            range_class_iri=p.range_class_iri,
            description=p.description,
            unit=p.unit,
            is_required=p.is_required,
            is_multivalued=p.is_multivalued,
            is_locked=p.is_locked,
            created_at=_to_iso(p.created_at),
        )
        for p in properties
    ]


@router.post("/{ontology_id}/properties", response_model=PropertyResponse, status_code=status.HTTP_201_CREATED)
async def create_property(
    ontology_id: uuid.UUID,
    data: PropertyCreate,
    session: AsyncSession = Depends(get_session),
) -> PropertyResponse:
    """创建属性"""
    # 本体验证
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    if not onto_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="本体不存在")

    # IRI 唯一性
    iri_result = await session.execute(
        select(Property).where(Property.iri == data.iri)
    )
    if iri_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="IRI 已存在")

    # 域类
    domain_id = None
    if data.domain_iri:
        domain_result = await session.execute(
            select(OntologyClass).where(
                and_(
                    OntologyClass.iri == data.domain_iri,
                    OntologyClass.ontology_id == ontology_id,
                )
            )
        )
        domain_class = domain_result.scalar_one_or_none()
        if domain_class:
            domain_id = domain_class.id

    # 值域类
    range_class_id = None
    if data.range_class_iri:
        range_result = await session.execute(
            select(OntologyClass).where(OntologyClass.iri == data.range_class_iri)
        )
        range_class = range_result.scalar_one_or_none()
        if range_class:
            range_class_id = range_class.id

    # 对象属性必须有 range_class_iri
    if (
        data.property_type == PropertyType.OBJECT_PROPERTY
        and not data.range_class_iri
    ):
        raise HTTPException(
            status_code=422,
            detail="对象属性必须有 range_class_iri",
        )

    property_obj = Property(
        ontology_id=ontology_id,
        name=data.name,
        local_name=data.local_name,
        iri=data.iri,
        property_type=data.property_type,
        domain_id=domain_id,
        domain_iri=data.domain_iri,
        range_type=data.range_type,
        range_class_id=range_class_id,
        range_class_iri=data.range_class_iri,
        description=data.description,
        unit=data.unit,
        is_required=data.is_required,
        is_multivalued=data.is_multivalued,
    )

    session.add(property_obj)
    await session.flush()
    await session.refresh(property_obj)

    return PropertyResponse(
        id=property_obj.id,
        name=property_obj.name,
        local_name=property_obj.local_name,
        iri=property_obj.iri,
        property_type=property_obj.property_type,
        domain_iri=property_obj.domain_iri,
        range_type=property_obj.range_type,
        range_class_iri=property_obj.range_class_iri,
        description=property_obj.description,
        unit=property_obj.unit,
        is_required=property_obj.is_required,
        is_multivalued=property_obj.is_multivalued,
        is_locked=property_obj.is_locked,
        created_at=_to_iso(property_obj.created_at),
    )


@router.patch("/{ontology_id}/properties/{property_id}", response_model=PropertyResponse)
async def update_property(
    ontology_id: uuid.UUID,
    property_id: uuid.UUID,
    data: PropertyUpdate,
    session: AsyncSession = Depends(get_session),
) -> PropertyResponse:
    """更新属性"""
    result = await session.execute(
        select(Property).where(
            and_(
                Property.id == property_id,
                Property.ontology_id == ontology_id,
            )
        )
    )
    prop = result.scalar_one_or_none()

    if not prop:
        raise HTTPException(status_code=404, detail="属性不存在")

    if prop.is_locked:
        raise HTTPException(status_code=400, detail="属性已锁定，无法修改")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(prop, field, value)

    await session.flush()
    await session.refresh(prop)

    return PropertyResponse(
        id=prop.id,
        name=prop.name,
        local_name=prop.local_name,
        iri=prop.iri,
        property_type=prop.property_type,
        domain_iri=prop.domain_iri,
        range_type=prop.range_type,
        range_class_iri=prop.range_class_iri,
        description=prop.description,
        unit=prop.unit,
        is_required=prop.is_required,
        is_multivalued=prop.is_multivalued,
        is_locked=prop.is_locked,
        created_at=_to_iso(prop.created_at),
    )


@router.delete("/{ontology_id}/properties/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(
    ontology_id: uuid.UUID,
    property_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除属性"""
    result = await session.execute(
        select(Property).where(
            and_(
                Property.id == property_id,
                Property.ontology_id == ontology_id,
            )
        )
    )
    prop = result.scalar_one_or_none()

    if not prop:
        raise HTTPException(status_code=404, detail="属性不存在")

    if prop.is_locked:
        raise HTTPException(status_code=400, detail="属性已锁定，无法删除")

    await session.delete(prop)


# =====================================================================
# 约束路由
# =====================================================================


@router.get("/{ontology_id}/constraints", response_model=list[ConstraintResponse])
async def list_constraints(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    target_class_iri: Optional[str] = Query(None),
    property_iri: Optional[str] = Query(None),
    severity: Optional[SeverityLevel] = Query(None),
) -> list[ConstraintResponse]:
    """列出约束"""
    query = select(Constraint).where(Constraint.ontology_id == ontology_id)

    if target_class_iri:
        query = query.where(Constraint.target_class_iri == target_class_iri)
    if property_iri:
        query = query.where(Constraint.property_iri == property_iri)
    if severity is not None:
        query = query.where(Constraint.severity == severity)

    query = query.order_by(Constraint.created_at.desc())

    result = await session.execute(query)
    constraints = result.scalars().all()

    return [
        ConstraintResponse(
            id=c.id,
            name=c.name,
            ontology_class_id=c.ontology_class_id,
            property_id=c.property_id,
            target_class_iri=c.target_class_iri,
            property_iri=c.property_iri,
            constraint_type=c.constraint_type,
            severity=c.severity,
            value=c.value,
            description=c.description,
            created_at=_to_iso(c.created_at),
        )
        for c in constraints
    ]


@router.post("/{ontology_id}/constraints", response_model=ConstraintResponse, status_code=status.HTTP_201_CREATED)
async def create_constraint(
    ontology_id: uuid.UUID,
    data: ConstraintCreate,
    session: AsyncSession = Depends(get_session),
) -> ConstraintResponse:
    """创建约束"""
    # 本体验证
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    if not onto_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="本体不存在")

    # 类验证（如果提供了 ontology_class_id）
    if data.ontology_class_id:
        class_result = await session.execute(
            select(OntologyClass).where(
                and_(
                    OntologyClass.id == data.ontology_class_id,
                    OntologyClass.ontology_id == ontology_id,
                )
            )
        )
        if not class_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="指定的类不存在")

    # 属性验证
    if data.property_id:
        prop_result = await session.execute(
            select(Property).where(
                and_(
                    Property.id == data.property_id,
                    Property.ontology_id == ontology_id,
                )
            )
        )
        if not prop_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="指定的属性不存在")

    constraint = Constraint(
        ontology_id=ontology_id,
        name=data.name,
        ontology_class_id=data.ontology_class_id,
        property_id=data.property_id,
        target_class_iri=data.target_class_iri,
        property_iri=data.property_iri,
        constraint_type=data.constraint_type,
        severity=data.severity,
        value=data.value,
        description=data.description,
    )

    session.add(constraint)
    await session.flush()
    await session.refresh(constraint)

    return ConstraintResponse(
        id=constraint.id,
        name=constraint.name,
        ontology_class_id=constraint.ontology_class_id,
        property_id=constraint.property_id,
        target_class_iri=constraint.target_class_iri,
        property_iri=constraint.property_iri,
        constraint_type=constraint.constraint_type,
        severity=constraint.severity,
        value=constraint.value,
        description=constraint.description,
        created_at=_to_iso(constraint.created_at),
    )


@router.get("/{ontology_id}/constraints/{constraint_id}", response_model=ConstraintResponse)
async def get_constraint(
    ontology_id: uuid.UUID,
    constraint_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ConstraintResponse:
    """获取约束"""
    result = await session.execute(
        select(Constraint).where(
            and_(
                Constraint.id == constraint_id,
                Constraint.ontology_id == ontology_id,
            )
        )
    )
    constraint = result.scalar_one_or_none()

    if not constraint:
        raise HTTPException(status_code=404, detail="约束不存在")

    return ConstraintResponse(
        id=constraint.id,
        name=constraint.name,
        ontology_class_id=constraint.ontology_class_id,
        property_id=constraint.property_id,
        target_class_iri=constraint.target_class_iri,
        property_iri=constraint.property_iri,
        constraint_type=constraint.constraint_type,
        severity=constraint.severity,
        value=constraint.value,
        description=constraint.description,
        created_at=_to_iso(constraint.created_at),
    )


@router.patch("/{ontology_id}/constraints/{constraint_id}", response_model=ConstraintResponse)
async def update_constraint(
    ontology_id: uuid.UUID,
    constraint_id: uuid.UUID,
    data: ConstraintUpdate,
    session: AsyncSession = Depends(get_session),
) -> ConstraintResponse:
    """更新约束"""
    result = await session.execute(
        select(Constraint).where(
            and_(
                Constraint.id == constraint_id,
                Constraint.ontology_id == ontology_id,
            )
        )
    )
    constraint = result.scalar_one_or_none()

    if not constraint:
        raise HTTPException(status_code=404, detail="约束不存在")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(constraint, field, value)

    await session.flush()
    await session.refresh(constraint)

    return ConstraintResponse(
        id=constraint.id,
        name=constraint.name,
        ontology_class_id=constraint.ontology_class_id,
        property_id=constraint.property_id,
        target_class_iri=constraint.target_class_iri,
        property_iri=constraint.property_iri,
        constraint_type=constraint.constraint_type,
        severity=constraint.severity,
        value=constraint.value,
        description=constraint.description,
        created_at=_to_iso(constraint.created_at),
    )


@router.delete("/{ontology_id}/constraints/{constraint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_constraint(
    ontology_id: uuid.UUID,
    constraint_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除约束"""
    result = await session.execute(
        select(Constraint).where(
            and_(
                Constraint.id == constraint_id,
                Constraint.ontology_id == ontology_id,
            )
        )
    )
    constraint = result.scalar_one_or_none()

    if not constraint:
        raise HTTPException(status_code=404, detail="约束不存在")

    await session.delete(constraint)


# =====================================================================
# 关系路由（基础，仅 CRUD，不含发布）
# =====================================================================


@router.get("/{ontology_id}/relations", response_model=list[RelationResponse])
async def list_relations(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    source_class_iri: Optional[str] = Query(None),
    target_class_iri: Optional[str] = Query(None),
) -> list[RelationResponse]:
    """列出关系"""
    query = select(Relation).where(Relation.ontology_id == ontology_id)

    if source_class_iri:
        query = query.where(Relation.source_class_iri == source_class_iri)
    if target_class_iri:
        query = query.where(Relation.target_class_iri == target_class_iri)

    query = query.order_by(Relation.name)

    result = await session.execute(query)
    relations = result.scalars().all()

    return [
        RelationResponse(
            id=r.id,
            name=r.name,
            local_name=r.local_name,
            iri=r.iri,
            relation_type=r.relation_type,
            source_class_iri=r.source_class_iri,
            target_class_iri=r.target_class_iri,
            is_required=r.is_required,
            is_transitive=r.is_transitive,
            is_symmetric=r.is_symmetric,
            is_inverse_functional=r.is_inverse_functional,
            description=r.description,
            created_at=_to_iso(r.created_at),
        )
        for r in relations
    ]


@router.post("/{ontology_id}/relations", response_model=RelationResponse, status_code=status.HTTP_201_CREATED)
async def create_relation(
    ontology_id: uuid.UUID,
    data: RelationCreate,
    session: AsyncSession = Depends(get_session),
) -> RelationResponse:
    """创建关系"""
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    if not onto_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="本体不存在")

    relation = Relation(
        ontology_id=ontology_id,
        name=data.name,
        local_name=data.local_name,
        iri=data.iri,
        relation_type=data.relation_type,
        source_class_iri=data.source_class_iri,
        target_class_iri=data.target_class_iri,
        is_required=data.is_required,
        is_transitive=data.is_transitive,
        is_symmetric=data.is_symmetric,
        is_inverse_functional=data.is_inverse_functional,
        description=data.description,
    )

    session.add(relation)
    await session.flush()
    await session.refresh(relation)

    return RelationResponse(
        id=relation.id,
        name=relation.name,
        local_name=relation.local_name,
        iri=relation.iri,
        relation_type=relation.relation_type,
        source_class_iri=relation.source_class_iri,
        target_class_iri=relation.target_class_iri,
        is_required=relation.is_required,
        is_transitive=relation.is_transitive,
        is_symmetric=relation.is_symmetric,
        is_inverse_functional=relation.is_inverse_functional,
        description=relation.description,
        created_at=_to_iso(relation.created_at),
    )


@router.delete("/{ontology_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relation(
    ontology_id: uuid.UUID,
    relation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除关系"""
    result = await session.execute(
        select(Relation).where(
            and_(
                Relation.id == relation_id,
                Relation.ontology_id == ontology_id,
            )
        )
    )
    relation = result.scalar_one_or_none()

    if not relation:
        raise HTTPException(status_code=404, detail="关系不存在")

    await session.delete(relation)


# =====================================================================
# Pydantic: 发布与导出
# =====================================================================


class PublishRequest(BaseModel):
    """发布请求"""

    version: str = Field(..., min_length=1, max_length=50)
    change_summary: Optional[str] = Field(None, max_length=1000)
    description: Optional[str] = Field(None, max_length=500)


class PublishResponse(BaseModel):
    """发布响应"""

    version_id: uuid.UUID
    version: str
    snapshot_class_count: int
    snapshot_property_count: int
    snapshot_relation_count: int
    snapshot_constraint_count: int
    published_at: str


class ExportFormat(str):
    """导出格式枚举"""

    TTL = "ttl"
    OWL = "owl"
    JSONLD = "jsonld"


class ExportRequest(BaseModel):
    """导出请求"""

    format: Literal["ttl", "owl", "jsonld"] = "ttl"
    include_shacl: bool = False


# =====================================================================
# 发布
# =====================================================================


def _serialize_class_for_version(cls: OntologyClass) -> dict:
    return {
        "name": cls.name,
        "local_name": cls.local_name,
        "iri": cls.iri,
        "class_type": cls.class_type.value if cls.class_type else ClassType.ONTOLOGY_CLASS.value,
        "parent_iri": cls.parent_iri,
        "level": cls.level,
        "description": cls.description,
        "definition": cls.definition,
        "examples": cls.examples or [],
        "enum_values": cls.enum_values or [],
        "alignment": cls.alignment,
    }


def _serialize_property_for_version(prop: Property) -> dict:
    return {
        "name": prop.name,
        "local_name": prop.local_name,
        "iri": prop.iri,
        "property_type": prop.property_type.value if prop.property_type else PropertyType.DATATYPE_PROPERTY.value,
        "domain_iri": prop.domain_iri,
        "range_type": prop.range_type,
        "range_class_iri": prop.range_class_iri,
        "description": prop.description,
        "unit": prop.unit,
        "is_required": prop.is_required,
        "is_multivalued": prop.is_multivalued,
    }


def _serialize_relation_for_version(rel: Relation) -> dict:
    return {
        "name": rel.name,
        "local_name": rel.local_name,
        "iri": rel.iri,
        "relation_type": rel.relation_type.value if rel.relation_type else RelationType.OBJECT.value,
        "source_class_iri": rel.source_class_iri,
        "target_class_iri": rel.target_class_iri,
        "is_required": rel.is_required,
        "is_transitive": rel.is_transitive,
        "is_symmetric": rel.is_symmetric,
        "is_inverse_functional": rel.is_inverse_functional,
        "description": rel.description,
    }


def _serialize_constraint_for_version(c: Constraint) -> dict:
    return {
        "name": c.name,
        "target_class_iri": c.target_class_iri,
        "property_iri": c.property_iri,
        "constraint_type": c.constraint_type.value if c.constraint_type else ConstraintType.CARDINALITY.value,
        "severity": c.severity.value if c.severity else SeverityLevel.WARNING.value,
        "value": c.value,
        "description": c.description,
    }


@router.post("/{ontology_id}/publish", response_model=PublishResponse)
async def publish_ontology(
    ontology_id: uuid.UUID,
    data: PublishRequest,
    session: AsyncSession = Depends(get_session),
) -> PublishResponse:
    """发布本体

    行为：
    - 校验所有 severity=violation 的约束均已解决（无未解决的 violation）
    - 生成 OntologyVersion 快照（序列化当前所有类/属性/关系/约束）
    - 将本体状态置为 published，version 字段填入新版本号
    """
    # 1. 找到本体
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()
    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    if ontology.status == OntologyStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="本体已是发布状态，请新建版本")

    if ontology.status == OntologyStatus.ARCHIVED:
        raise HTTPException(status_code=400, detail="已归档的本体不能发布")

    # 2. 校验未解决的 violation
    violation_result = await session.execute(
        select(func.count(Constraint.id)).where(
            and_(
                Constraint.ontology_id == ontology_id,
                Constraint.severity == SeverityLevel.VIOLATION,
            )
        )
    )
    violation_count = violation_result.scalar() or 0
    if violation_count > 0:
        raise HTTPException(
            status_code=422,
            detail=f"存在 {violation_count} 条未解决的 violation 约束，阻塞发布",
        )

    # 3. 查询快照数据
    classes_result = await session.execute(
        select(OntologyClass).where(OntologyClass.ontology_id == ontology_id)
    )
    classes = list(classes_result.scalars().all())

    props_result = await session.execute(
        select(Property).where(Property.ontology_id == ontology_id)
    )
    properties = list(props_result.scalars().all())

    rels_result = await session.execute(
        select(Relation).where(Relation.ontology_id == ontology_id)
    )
    relations = list(rels_result.scalars().all())

    cons_result = await session.execute(
        select(Constraint).where(Constraint.ontology_id == ontology_id)
    )
    constraints = list(cons_result.scalars().all())

    # 4. 创建版本快照
    version_record = OntologyVersion(
        ontology_id=ontology_id,
        version=data.version,
        status=OntologyVersionStatus.PUBLISHED,
        change_summary=data.change_summary,
        change_details={
            "class_count": len(classes),
            "property_count": len(properties),
            "relation_count": len(relations),
            "constraint_count": len(constraints),
            "description": data.description,
        },
        published_at=datetime.now(timezone.utc),
        class_snapshot=[_serialize_class_for_version(c) for c in classes],
        property_snapshot=[_serialize_property_for_version(p) for p in properties],
        relation_snapshot=[_serialize_relation_for_version(r) for r in relations],
        constraint_snapshot=[_serialize_constraint_for_version(c) for c in constraints],
    )
    session.add(version_record)

    # 5. 更新本体
    ontology.version = data.version
    ontology.status = OntologyStatus.PUBLISHED
    ontology.class_count = len(classes)
    ontology.property_count = len(properties)

    await session.flush()
    await session.refresh(version_record)

    return PublishResponse(
        version_id=version_record.id,
        version=data.version,
        snapshot_class_count=len(classes),
        snapshot_property_count=len(properties),
        snapshot_relation_count=len(relations),
        snapshot_constraint_count=len(constraints),
        published_at=version_record.published_at.isoformat() if version_record.published_at else "",
    )


# =====================================================================
# 导出
# =====================================================================


def _ns_prefix(uri: str) -> str:
    """从 URI 提取短前缀（如 https://example.org/onto# → onto）"""
    if "#" in uri:
        return uri.rsplit("#", 1)[0].split("/")[-1]
    if "/" in uri:
        return uri.rstrip("/").split("/")[-1]
    return uri


def _local_name(uri: str) -> str:
    if "#" in uri:
        return uri.rsplit("#", 1)[1]
    if "/" in uri:
        return uri.rsplit("/", 1)[1]
    return uri


def _to_turtle(ontology: Ontology, classes: list, properties: list, relations: list) -> str:
    """生成 Turtle (TTL) 格式"""
    ns_map: dict[str, str] = {}
    ns_counter: dict[str, int] = {}

    def get_prefix(uri: str) -> str:
        base = _ns_prefix(uri)
        if base not in ns_map:
            if base in ns_counter:
                ns_counter[base] += 1
                base = f"{base}{ns_counter[base]}"
            else:
                ns_counter[base] = 0
            ns_map[uri.rsplit("#", 1)[0] if "#" in uri else uri.rstrip("/").rsplit("/", 1)[0]] = base
        return ns_map.get(
            uri.rsplit("#", 1)[0] if "#" in uri else uri.rstrip("/").rsplit("/", 1)[0],
            base,
        )

    lines = ["@prefix owl: <http://www.w3.org/2002/07/owl#> .",
             "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
             "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
             "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
             ""]

    for uri, prefix in ns_map.items():
        lines.append(f"@prefix {prefix}: <{uri}/> .")
    if ns_map:
        lines.append("")

    lines.append(f"###  {ontology.name}")
    lines.append(f"<{ontology.namespace}> a owl:Ontology ;")
    lines.append(f'    rdfs:label "{ontology.name}" ;')
    if ontology.description:
        lines.append(f'    rdfs:comment "{ontology.description}" ;')
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    lines.append("")

    for c in classes:
        prefix = get_prefix(c.iri)
        ln = _local_name(c.iri)
        lines.append(f"###  Class: {c.name}")
        lines.append(f"{prefix}:{ln} a owl:Class ;")
        if c.parent_iri:
            p_prefix = get_prefix(c.parent_iri)
            p_ln = _local_name(c.parent_iri)
            lines.append(f"    rdfs:subClassOf {p_prefix}:{p_ln} ;")
        if c.description:
            lines.append(f'    rdfs:comment "{c.description}" ;')
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("")

    for p in properties:
        prefix = get_prefix(p.iri)
        ln = _local_name(p.iri)
        pt = "owl:DatatypeProperty" if p.property_type == PropertyType.DATATYPE_PROPERTY else "owl:ObjectProperty"
        lines.append(f"###  Property: {p.name}")
        lines.append(f"{prefix}:{ln} a {pt} ;")
        if p.domain_iri:
            d_prefix = get_prefix(p.domain_iri)
            d_ln = _local_name(p.domain_iri)
            lines.append(f"    rdfs:domain {d_prefix}:{d_ln} ;")
        if p.range_type:
            lines.append(f'    rdfs:range xsd:{p.range_type.replace("xsd:", "")} ;')
        elif p.range_class_iri:
            r_prefix = get_prefix(p.range_class_iri)
            r_ln = _local_name(p.range_class_iri)
            lines.append(f"    rdfs:range {r_prefix}:{r_ln} ;")
        if p.description:
            lines.append(f'    rdfs:comment "{p.description}" ;')
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("")

    for r in relations:
        prefix = get_prefix(r.iri)
        ln = _local_name(r.iri)
        lines.append(f"###  Relation: {r.name}")
        lines.append(f"{prefix}:{ln} a owl:ObjectProperty ;")
        if r.source_class_iri:
            s_prefix = get_prefix(r.source_class_iri)
            s_ln = _local_name(r.source_class_iri)
            lines.append(f"    rdfs:domain {s_prefix}:{s_ln} ;")
        if r.target_class_iri:
            t_prefix = get_prefix(r.target_class_iri)
            t_ln = _local_name(r.target_class_iri)
            lines.append(f"    rdfs:range {t_prefix}:{t_ln} ;")
        if r.description:
            lines.append(f'    rdfs:comment "{r.description}" ;')
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("")

    return "\n".join(lines)


def _to_owl(ontology: Ontology, classes: list, properties: list, relations: list) -> dict:
    """生成 OWL/RDF JSON 兼容结构"""
    def _iri(uri: str) -> dict:
        return {"@id": uri}

    def _lit(v: str) -> dict:
        return {"@value": v, "@type": "http://www.w3.org/2001/XMLSchema#string"}

    def _lang(v: str, lang: str = "zh") -> dict:
        return {"@value": v, "@language": lang}

    ents = []

    # Ontology
    ontology_entry = {
        "@id": ontology.namespace,
        "@type": ["http://www.w3.org/2002/07/owl#Ontology"],
        "http://www.w3.org/2000/01/rdf-schema#label": [_lit(ontology.name)],
    }
    if ontology.description:
        ontology_entry["http://www.w3.org/2000/01/rdf-schema#comment"] = [_lit(ontology.description)]
    ents.append(ontology_entry)

    for c in classes:
        ce = {
            "@id": c.iri,
            "@type": ["http://www.w3.org/2002/07/owl#Class"],
        }
        if c.parent_iri:
            ce["http://www.w3.org/2000/01/rdf-schema#subClassOf"] = [{"@id": c.parent_iri}]
        if c.description:
            ce["http://www.w3.org/2000/01/rdf-schema#comment"] = [_lit(c.description)]
        ents.append(ce)

    for p in properties:
        pt = (
            "http://www.w3.org/2002/07/owl#DatatypeProperty"
            if p.property_type == PropertyType.DATATYPE_PROPERTY
            else "http://www.w3.org/2002/07/owl#ObjectProperty"
        )
        pe = {"@id": p.iri, "@type": [pt]}
        if p.domain_iri:
            pe["http://www.w3.org/2000/01/rdf-schema#domain"] = [_iri(p.domain_iri)]
        if p.range_type:
            pe["http://www.w3.org/2000/01/rdf-schema#range"] = [_iri(f"http://www.w3.org/2001/XMLSchema#{p.range_type.replace('xsd:', '')}")]
        elif p.range_class_iri:
            pe["http://www.w3.org/2000/01/rdf-schema#range"] = [_iri(p.range_class_iri)]
        if p.description:
            pe["http://www.w3.org/2000/01/rdf-schema#comment"] = [_lit(p.description)]
        ents.append(pe)

    for r in relations:
        re_ = {"@id": r.iri, "@type": ["http://www.w3.org/2002/07/owl#ObjectProperty"]}
        if r.source_class_iri:
            re_["http://www.w3.org/2000/01/rdf-schema#domain"] = [_iri(r.source_class_iri)]
        if r.target_class_iri:
            re_["http://www.w3.org/2000/01/rdf-schema#range"] = [_iri(r.target_class_iri)]
        if r.description:
            re_["http://www.w3.org/2000/01/rdf-schema#comment"] = [_lit(r.description)]
        ents.append(re_)

    return {"@graph": ents, "@context": {
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }}


def _to_jsonld(ontology: Ontology, classes: list, properties: list, relations: list) -> dict:
    """生成 JSON-LD"""
    doc = _to_owl(ontology, classes, properties, relations)
    doc["@context"] = {
        "@vocab": ontology.namespace + "#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "label": {"@id": "http://www.w3.org/2000/01/rdf-schema#label"},
        "comment": {"@id": "http://www.w3.org/2000/01/rdf-schema#comment"},
        "subClassOf": {"@id": "http://www.w3.org/2000/01/rdf-schema#subClassOf"},
        "domain": {"@id": "http://www.w3.org/2000/01/rdf-schema#domain"},
        "range": {"@id": "http://www.w3.org/2000/01/rdf-schema#range"},
    }
    return doc


@router.post("/{ontology_id}/export")
async def export_ontology(
    ontology_id: uuid.UUID,
    data: ExportRequest,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """导出本体为 OWL / Turtle / JSON-LD

    同步生成，非异步任务（中小本体足够快）。
    - ttl: text/turtle
    - owl: application/ld+json（RDF/JSON 格式）
    - jsonld: application/ld+json（JSON-LD 格式）
    """
    result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = result.scalar_one_or_none()
    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    classes_result = await session.execute(
        select(OntologyClass).where(OntologyClass.ontology_id == ontology_id)
    )
    classes = list(classes_result.scalars().all())

    props_result = await session.execute(
        select(Property).where(Property.ontology_id == ontology_id)
    )
    properties = list(props_result.scalars().all())

    rels_result = await session.execute(
        select(Relation).where(Relation.ontology_id == ontology_id)
    )
    relations = list(rels_result.scalars().all())

    if data.format == "ttl":
        content = _to_turtle(ontology, classes, properties, relations)
        return Response(
            content=content,
            media_type="text/turtle",
            headers={"Content-Disposition": f'attachment; filename="{ontology.name}.ttl"'},
        )
    elif data.format == "owl":
        content = _to_owl(ontology, classes, properties, relations)
        import json
        return Response(
            content=json.dumps(content, ensure_ascii=False, indent=2),
            media_type="application/ld+json",
            headers={"Content-Disposition": f'attachment; filename="{ontology.name}.owl.json"'},
        )
    else:  # jsonld
        content = _to_jsonld(ontology, classes, properties, relations)
        import json
        return Response(
            content=json.dumps(content, ensure_ascii=False, indent=2),
            media_type="application/ld+json",
            headers={"Content-Disposition": f'attachment; filename="{ontology.name}.jsonld"'},
        )


# =====================================================================
# 版本历史
# =====================================================================


class OntologyVersionResponse(BaseModel):
    id: uuid.UUID
    ontology_id: uuid.UUID
    version: str
    status: OntologyVersionStatus
    is_baseline: bool
    change_summary: Optional[str]
    published_at: Optional[str]
    class_count: int = 0
    property_count: int = 0
    relation_count: int = 0
    constraint_count: int = 0

    model_config = {"from_attributes": True}


@router.get("/{ontology_id}/versions", response_model=list[OntologyVersionResponse])
async def list_versions(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[OntologyVersionResponse]:
    """列出本体版本历史"""
    result = await session.execute(
        select(OntologyVersion)
        .where(OntologyVersion.ontology_id == ontology_id)
        .order_by(OntologyVersion.published_at.desc())
    )
    versions = result.scalars().all()

    return [
        OntologyVersionResponse(
            id=v.id,
            ontology_id=v.ontology_id,
            version=v.version,
            status=v.status,
            is_baseline=v.is_baseline,
            change_summary=v.change_summary,
            published_at=_to_iso(v.published_at),
            class_count=len(v.class_snapshot) if v.class_snapshot else 0,
            property_count=len(v.property_snapshot) if v.property_snapshot else 0,
            relation_count=len(v.relation_snapshot) if v.relation_snapshot else 0,
            constraint_count=len(v.constraint_snapshot) if v.constraint_snapshot else 0,
        )
        for v in versions
    ]


@router.get("/{ontology_id}/versions/{version_id}", response_model=OntologyVersionResponse)
async def get_version(
    ontology_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> OntologyVersionResponse:
    """获取特定版本详情（含快照内容）"""
    result = await session.execute(
        select(OntologyVersion).where(
            and_(
                OntologyVersion.id == version_id,
                OntologyVersion.ontology_id == ontology_id,
            )
        )
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")

    return OntologyVersionResponse(
        id=v.id,
        ontology_id=v.ontology_id,
        version=v.version,
        status=v.status,
        is_baseline=v.is_baseline,
        change_summary=v.change_summary,
        published_at=_to_iso(v.published_at),
        class_count=len(v.class_snapshot) if v.class_snapshot else 0,
        property_count=len(v.property_snapshot) if v.property_snapshot else 0,
        relation_count=len(v.relation_snapshot) if v.relation_snapshot else 0,
        constraint_count=len(v.constraint_snapshot) if v.constraint_snapshot else 0,
    )


# =====================================================================
# Fork（分支）— HIA-56 / A7 缺失端点
# =====================================================================


class ForkRequest(BaseModel):
    """Fork 请求"""

    name: str = Field(..., min_length=1, max_length=255)
    namespace: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=1000)
    version_tag: str = Field(default="fork-1", max_length=50)


class ForkResponse(BaseModel):
    """Fork 响应"""

    forked_ontology_id: uuid.UUID
    forked_ontology_name: str
    forked_namespace: str
    forked_version_id: uuid.UUID
    forked_version: str
    parent_ontology_id: uuid.UUID
    parent_version: Optional[str]
    class_count: int
    property_count: int


def _fork_iri(old_iri: str, old_namespace: str, new_namespace: str) -> str:
    """把旧 IRI 中的 old_namespace 替换为 new_namespace。"""
    if old_iri.startswith(old_namespace):
        return new_namespace.rstrip("/#") + "/" + old_iri[len(old_namespace):].lstrip("/#")
    # 尝试用 rsplit
    if "/" in old_iri or "#" in old_iri:
        sep = "#" if "#" in old_iri else "/"
        return new_namespace.rstrip("/#") + sep + old_iri.split(sep)[-1]
    return old_iri


@router.post("/{ontology_id}/fork", response_model=ForkResponse, status_code=status.HTTP_201_CREATED)
async def fork_ontology(
    ontology_id: uuid.UUID,
    data: ForkRequest,
    session: AsyncSession = Depends(get_session),
) -> ForkResponse:
    """Fork 本体

    从当前本体复制一个完整副本：
    - 创建新的 Ontology（draft 状态）
    - 复制所有 OntologyClass / Property / Relation / Constraint（IRI 按新 namespace 重写）
    - 创建首个 OntologyVersion 快照（status=draft）
    - 返回新本体信息

    用于实验性分支、基于参考本体派生等场景。
    """
    # 1. 找到源本体
    src_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    src = src_result.scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="源本体不存在")

    # 2. 创建新的 Ontology
    forked = Ontology(
        name=data.name,
        namespace=data.namespace,
        description=data.description or src.description,
        project_id=src.project_id,
        kind=src.kind,
        status=OntologyStatus.DRAFT,
        standard_name=src.standard_name,
        source_url=src.source_url,
        source_format=src.source_format,
        version=data.version_tag,
        class_count=src.class_count,
        property_count=src.property_count,
        created_by=getattr(src, "created_by", None),
    )
    session.add(forked)
    await session.flush()
    await session.refresh(forked)

    old_ns = src.namespace

    # 3. 复制类
    src_classes = list((await session.execute(
        select(OntologyClass).where(OntologyClass.ontology_id == ontology_id)
    )).scalars().all())

    old_iri_to_new: dict[str, str] = {}
    for c in src_classes:
        new_iri = _fork_iri(c.iri, old_ns, data.namespace)
        old_iri_to_new[c.iri] = new_iri

        new_class = OntologyClass(
            ontology_id=forked.id,
            name=c.name,
            local_name=c.local_name,
            iri=new_iri,
            class_type=c.class_type,
            parent_iri=c.parent_iri,
            level=c.level,
            description=c.description,
            definition=c.definition,
            examples=c.examples,
            enum_values=c.enum_values,
            alignment=c.alignment,
            standard_mappings=c.standard_mappings,
            created_by=c.created_by,
        )
        session.add(new_class)

    await session.flush()

    # 4. 复制属性
    src_props = list((await session.execute(
        select(Property).where(Property.ontology_id == ontology_id)
    )).scalars().all())

    for p in src_props:
        new_iri = _fork_iri(p.iri, old_ns, data.namespace)
        # 映射 domain_iri / range_class_iri
        new_domain_iri = _fork_iri(p.domain_iri, old_ns, data.namespace) if p.domain_iri else None
        new_range_iri = _fork_iri(p.range_class_iri, old_ns, data.namespace) if p.range_class_iri else None

        new_prop = Property(
            ontology_id=forked.id,
            name=p.name,
            local_name=p.local_name,
            iri=new_iri,
            property_type=p.property_type,
            domain_iri=new_domain_iri,
            range_type=p.range_type,
            range_class_iri=new_range_iri,
            description=p.description,
            unit=p.unit,
            is_required=p.is_required,
            is_multivalued=p.is_multivalued,
            standard_mappings=p.standard_mappings,
            created_by=p.created_by,
        )
        session.add(new_prop)

    # 5. 复制关系
    src_rels = list((await session.execute(
        select(Relation).where(Relation.ontology_id == ontology_id)
    )).scalars().all())

    for r in src_rels:
        new_iri = _fork_iri(r.iri, old_ns, data.namespace)
        new_src_iri = _fork_iri(r.source_class_iri, old_ns, data.namespace) if r.source_class_iri else None
        new_tgt_iri = _fork_iri(r.target_class_iri, old_ns, data.namespace) if r.target_class_iri else None

        new_rel = Relation(
            ontology_id=forked.id,
            name=r.name,
            local_name=r.local_name,
            iri=new_iri,
            relation_type=r.relation_type,
            source_class_iri=new_src_iri,
            target_class_iri=new_tgt_iri,
            is_required=r.is_required,
            is_transitive=r.is_transitive,
            is_symmetric=r.is_symmetric,
            is_inverse_functional=r.is_inverse_functional,
            description=r.description,
            created_by=r.created_by,
        )
        session.add(new_rel)

    # 6. 复制约束
    src_cons = list((await session.execute(
        select(Constraint).where(Constraint.ontology_id == ontology_id)
    )).scalars().all())

    for c in src_cons:
        new_target = _fork_iri(c.target_class_iri, old_ns, data.namespace) if c.target_class_iri else None
        new_prop_iri = _fork_iri(c.property_iri, old_ns, data.namespace) if c.property_iri else None

        new_con = Constraint(
            ontology_id=forked.id,
            name=c.name,
            ontology_class_id=None,  # 新类 ID 需要查表，重新关联
            property_id=None,
            target_class_iri=new_target,
            property_iri=new_prop_iri,
            constraint_type=c.constraint_type,
            severity=c.severity,
            value=c.value,
            description=c.description,
        )
        session.add(new_con)

    # 7. 创建 fork 版本快照
    fork_version = OntologyVersion(
        ontology_id=forked.id,
        version=data.version_tag,
        status=OntologyVersionStatus.DRAFT,
        is_baseline=False,
        baseline_of=ontology_id,
        change_summary=f"Forked from {src.name} (v{src.version or 'N/A'})",
        class_snapshot=[_serialize_class_for_version(c) for c in src_classes] if src_classes else None,
        property_snapshot=[_serialize_property_for_version(p) for p in src_props] if src_props else None,
        relation_snapshot=[_serialize_relation_for_version(r) for r in src_rels] if src_rels else None,
        constraint_snapshot=[_serialize_constraint_for_version(c) for c in src_cons] if src_cons else None,
    )
    session.add(fork_version)

    await session.flush()
    await session.refresh(fork_version)

    return ForkResponse(
        forked_ontology_id=forked.id,
        forked_ontology_name=forked.name,
        forked_namespace=forked.namespace,
        forked_version_id=fork_version.id,
        forked_version=fork_version.version,
        parent_ontology_id=ontology_id,
        parent_version=src.version,
        class_count=len(src_classes),
        property_count=len(src_props),
    )


# =====================================================================
# Diff（版本对比）— HIA-56 / A7 缺失端点
# =====================================================================


class DiffClassEntry(BaseModel):
    iri: str
    name: str
    change: str  # added | removed | unchanged | modified


class DiffPropertyEntry(BaseModel):
    iri: str
    name: str
    domain_iri: Optional[str]
    change: str


class DiffRelationEntry(BaseModel):
    iri: str
    name: str
    source_class_iri: Optional[str]
    target_class_iri: Optional[str]
    change: str


class DiffConstraintEntry(BaseModel):
    iri: Optional[str]
    target_class_iri: Optional[str]
    property_iri: Optional[str]
    constraint_type: str
    change: str


class OntologyDiffResponse(BaseModel):
    from_version_id: uuid.UUID
    from_version: str
    to_version_id: uuid.UUID
    to_version: str
    class_diff: list[DiffClassEntry]
    property_diff: list[DiffPropertyEntry]
    relation_diff: list[DiffRelationEntry]
    constraint_diff: list[DiffConstraintEntry]
    summary: dict


@router.get("/{ontology_id}/diff", response_model=OntologyDiffResponse)
async def diff_ontology_versions(
    ontology_id: uuid.UUID,
    from_version: str = Query(..., description="源版本标签，如 'v1.0'"),
    to_version: str = Query(..., description="目标版本标签，如 'v2.0'"),
    session: AsyncSession = Depends(get_session),
) -> OntologyDiffResponse:
    """对比本体两个版本

    行为：
    - 按 ontology_id 和 version 标签找两个 OntologyVersion
    - 对比 class_snapshot / property_snapshot / relation_snapshot / constraint_snapshot
    - 返回每个维度的增/删/改列表 + 汇总统计
    """
    from_v_result = await session.execute(
        select(OntologyVersion).where(
            and_(
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.version == from_version,
            )
        )
    )
    from_v = from_v_result.scalar_one_or_none()
    if not from_v:
        raise HTTPException(status_code=404, detail=f"源版本 '{from_version}' 不存在")

    to_v_result = await session.execute(
        select(OntologyVersion).where(
            and_(
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.version == to_version,
            )
        )
    )
    to_v = to_v_result.scalar_one_or_none()
    if not to_v:
        raise HTTPException(status_code=404, detail=f"目标版本 '{to_version}' 不存在")

    # ---- 类对比 ----
    from_classes = {c["iri"]: c for c in (from_v.class_snapshot or [])}
    to_classes = {c["iri"]: c for c in (to_v.class_snapshot or [])}

    class_diff: list[DiffClassEntry] = []
    for iri, c in to_classes.items():
        if iri not in from_classes:
            class_diff.append(DiffClassEntry(iri=iri, name=c.get("name", ""), change="added"))
        else:
            from_c = from_classes[iri]
            # 简单修改检测：比对 name / description / definition
            if c.get("name") != from_c.get("name") or c.get("description") != from_c.get("description"):
                class_diff.append(DiffClassEntry(iri=iri, name=c.get("name", ""), change="modified"))
            else:
                class_diff.append(DiffClassEntry(iri=iri, name=c.get("name", ""), change="unchanged"))
    for iri in from_classes:
        if iri not in to_classes:
            class_diff.append(DiffClassEntry(iri=iri, name=from_classes[iri].get("name", ""), change="removed"))

    # ---- 属性对比 ----
    from_props = {p["iri"]: p for p in (from_v.property_snapshot or [])}
    to_props = {p["iri"]: p for p in (to_v.property_snapshot or [])}

    property_diff: list[DiffPropertyEntry] = []
    for iri, p in to_props.items():
        if iri not in from_props:
            property_diff.append(DiffPropertyEntry(iri=iri, name=p.get("name", ""), domain_iri=p.get("domain_iri"), change="added"))
        else:
            from_p = from_props[iri]
            if p != from_p:
                property_diff.append(DiffPropertyEntry(iri=iri, name=p.get("name", ""), domain_iri=p.get("domain_iri"), change="modified"))
            else:
                property_diff.append(DiffPropertyEntry(iri=iri, name=p.get("name", ""), domain_iri=p.get("domain_iri"), change="unchanged"))
    for iri in from_props:
        if iri not in to_props:
            property_diff.append(DiffPropertyEntry(iri=iri, name=from_props[iri].get("name", ""), domain_iri=from_props[iri].get("domain_iri"), change="removed"))

    # ---- 关系对比 ----
    from_rels = {r["iri"]: r for r in (from_v.relation_snapshot or [])}
    to_rels = {r["iri"]: r for r in (to_v.relation_snapshot or [])}

    relation_diff: list[DiffRelationEntry] = []
    for iri, r in to_rels.items():
        if iri not in from_rels:
            relation_diff.append(DiffRelationEntry(iri=iri, name=r.get("name", ""), source_class_iri=r.get("source_class_iri"), target_class_iri=r.get("target_class_iri"), change="added"))
        else:
            from_r = from_rels[iri]
            if r != from_r:
                relation_diff.append(DiffRelationEntry(iri=iri, name=r.get("name", ""), source_class_iri=r.get("source_class_iri"), target_class_iri=r.get("target_class_iri"), change="modified"))
            else:
                relation_diff.append(DiffRelationEntry(iri=iri, name=r.get("name", ""), source_class_iri=r.get("source_class_iri"), target_class_iri=r.get("target_class_iri"), change="unchanged"))
    for iri in from_rels:
        if iri not in to_rels:
            relation_diff.append(DiffRelationEntry(iri=iri, name=from_rels[iri].get("name", ""), source_class_iri=from_rels[iri].get("source_class_iri"), target_class_iri=from_rels[iri].get("target_class_iri"), change="removed"))

    # ---- 约束对比 ----
    from_cons = {(c.get("target_class_iri") or "", c.get("property_iri") or "", c.get("constraint_type") or ""): c for c in (from_v.constraint_snapshot or [])}
    to_cons = {(c.get("target_class_iri") or "", c.get("property_iri") or "", c.get("constraint_type") or ""): c for c in (to_v.constraint_snapshot or [])}

    constraint_diff: list[DiffConstraintEntry] = []
    for key in to_cons:
        if key not in from_cons:
            c = to_cons[key]
            constraint_diff.append(DiffConstraintEntry(iri=None, target_class_iri=c.get("target_class_iri"), property_iri=c.get("property_iri"), constraint_type=c.get("constraint_type", ""), change="added"))
        else:
            if to_cons[key] != from_cons[key]:
                constraint_diff.append(DiffConstraintEntry(iri=None, target_class_iri=to_cons[key].get("target_class_iri"), property_iri=to_cons[key].get("property_iri"), constraint_type=to_cons[key].get("constraint_type", ""), change="modified"))
            else:
                constraint_diff.append(DiffConstraintEntry(iri=None, target_class_iri=to_cons[key].get("target_class_iri"), property_iri=to_cons[key].get("property_iri"), constraint_type=to_cons[key].get("constraint_type", ""), change="unchanged"))
    for key in from_cons:
        if key not in to_cons:
            c = from_cons[key]
            constraint_diff.append(DiffConstraintEntry(iri=None, target_class_iri=c.get("target_class_iri"), property_iri=c.get("property_iri"), constraint_type=c.get("constraint_type", ""), change="removed"))

    summary = {
        "classes": {
            "added": sum(1 for e in class_diff if e.change == "added"),
            "removed": sum(1 for e in class_diff if e.change == "removed"),
            "modified": sum(1 for e in class_diff if e.change == "modified"),
            "unchanged": sum(1 for e in class_diff if e.change == "unchanged"),
        },
        "properties": {
            "added": sum(1 for e in property_diff if e.change == "added"),
            "removed": sum(1 for e in property_diff if e.change == "removed"),
            "modified": sum(1 for e in property_diff if e.change == "modified"),
            "unchanged": sum(1 for e in property_diff if e.change == "unchanged"),
        },
        "relations": {
            "added": sum(1 for e in relation_diff if e.change == "added"),
            "removed": sum(1 for e in relation_diff if e.change == "removed"),
            "modified": sum(1 for e in relation_diff if e.change == "modified"),
            "unchanged": sum(1 for e in relation_diff if e.change == "unchanged"),
        },
        "constraints": {
            "added": sum(1 for e in constraint_diff if e.change == "added"),
            "removed": sum(1 for e in constraint_diff if e.change == "removed"),
            "modified": sum(1 for e in constraint_diff if e.change == "modified"),
            "unchanged": sum(1 for e in constraint_diff if e.change == "unchanged"),
        },
    }

    return OntologyDiffResponse(
        from_version_id=from_v.id,
        from_version=from_v.version,
        to_version_id=to_v.id,
        to_version=to_v.version,
        class_diff=class_diff,
        property_diff=property_diff,
        relation_diff=relation_diff,
        constraint_diff=constraint_diff,
        summary=summary,
    )


# =====================================================================
# Version CRUD（HIA-56 / A7）
# =====================================================================


class CreateVersionRequest(BaseModel):
    """创建草稿版本请求"""

    version: str = Field(
        default="draft",
        min_length=1,
        max_length=50,
        description="版本标签，如 'draft-1' 或 'v0.2.0'",
    )
    change_summary: Optional[str] = Field(
        None, max_length=1000, description="版本变更说明"
    )


class UpdateVersionContentRequest(BaseModel):
    """更新版本快照内容（编辑器保存）"""

    class_snapshot: Optional[list[dict]] = None
    property_snapshot: Optional[list[dict]] = None
    relation_snapshot: Optional[list[dict]] = None
    constraint_snapshot: Optional[list[dict]] = None
    change_summary: Optional[str] = Field(None, max_length=1000)


class PublishVersionRequest(BaseModel):
    """发布版本请求"""

    change_summary: Optional[str] = Field(None, max_length=1000)


class VersionDetailResponse(BaseModel):
    """含快照内容的版本详情"""

    id: uuid.UUID
    ontology_id: uuid.UUID
    version: str
    status: OntologyVersionStatus
    is_baseline: bool
    change_summary: Optional[str]
    published_at: Optional[str]
    created_at: str
    updated_at: str
    class_snapshot: Optional[list] = []
    property_snapshot: Optional[list] = []
    relation_snapshot: Optional[list] = []
    constraint_snapshot: Optional[list] = []
    class_count: int = 0
    property_count: int = 0
    relation_count: int = 0
    constraint_count: int = 0

    model_config = {"from_attributes": True}


@router.post(
    "/{ontology_id}/versions",
    response_model=VersionDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_draft_version(
    ontology_id: uuid.UUID,
    data: CreateVersionRequest = None,
    session: AsyncSession = Depends(get_session),
) -> VersionDetailResponse:
    """基于当前 head 创建新草稿版本（fork from published head）

    行为：
    - 找到当前已发布的最新版本作为 parent（若无则创建空快照）
    - 将其快照内容复制到新的 DRAFT 版本
    """
    # 1. 找本体
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = onto_result.scalar_one_or_none()
    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    # 2. 找最新发布的版本作为 parent
    parent_result = await session.execute(
        select(OntologyVersion)
        .where(
            and_(
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.status == OntologyVersionStatus.PUBLISHED,
            )
        )
        .order_by(OntologyVersion.published_at.desc())
        .limit(1)
    )
    parent = parent_result.scalar_one_or_none()

    # 3. 创建新草稿版本
    version_tag = (data.version or "draft") if data else "draft"

    new_version = OntologyVersion(
        ontology_id=ontology_id,
        version=version_tag,
        status=OntologyVersionStatus.DRAFT,
        change_summary=data.change_summary if data else None,
        # 从 parent 复制快照
        class_snapshot=(
            list(parent.class_snapshot) if parent and parent.class_snapshot else []
        ),
        property_snapshot=(
            list(parent.property_snapshot) if parent and parent.property_snapshot else []
        ),
        relation_snapshot=(
            list(parent.relation_snapshot) if parent and parent.relation_snapshot else []
        ),
        constraint_snapshot=(
            list(parent.constraint_snapshot)
            if parent and parent.constraint_snapshot
            else []
        ),
        baseline_of=parent.id if parent else None,
    )
    session.add(new_version)
    await session.flush()
    await session.refresh(new_version)

    return VersionDetailResponse(
        id=new_version.id,
        ontology_id=new_version.ontology_id,
        version=new_version.version,
        status=new_version.status,
        is_baseline=new_version.is_baseline,
        change_summary=new_version.change_summary,
        published_at=_to_iso(new_version.published_at),
        created_at=_to_iso(new_version.created_at),
        updated_at=_to_iso(new_version.updated_at),
        class_snapshot=new_version.class_snapshot or [],
        property_snapshot=new_version.property_snapshot or [],
        relation_snapshot=new_version.relation_snapshot or [],
        constraint_snapshot=new_version.constraint_snapshot or [],
        class_count=len(new_version.class_snapshot or []),
        property_count=len(new_version.property_snapshot or []),
        relation_count=len(new_version.relation_snapshot or []),
        constraint_count=len(new_version.constraint_snapshot or []),
    )


@router.put(
    "/{ontology_id}/versions/{version_id}/content",
    response_model=VersionDetailResponse,
)
async def update_version_content(
    ontology_id: uuid.UUID,
    version_id: uuid.UUID,
    data: UpdateVersionContentRequest,
    session: AsyncSession = Depends(get_session),
) -> VersionDetailResponse:
    """更新版本快照内容（编辑器保存）

    行为：
    - 仅允许更新 DRAFT 状态的版本
    - 整体替换快照字段
    """
    result = await session.execute(
        select(OntologyVersion).where(
            and_(
                OntologyVersion.id == version_id,
                OntologyVersion.ontology_id == ontology_id,
            )
        )
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")

    if v.status != OntologyVersionStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail=f"只能修改 DRAFT 状态的版本，当前状态：{v.status.value}",
        )

    # 整体替换快照
    if data.class_snapshot is not None:
        v.class_snapshot = data.class_snapshot
    if data.property_snapshot is not None:
        v.property_snapshot = data.property_snapshot
    if data.relation_snapshot is not None:
        v.relation_snapshot = data.relation_snapshot
    if data.constraint_snapshot is not None:
        v.constraint_snapshot = data.constraint_snapshot
    if data.change_summary is not None:
        v.change_summary = data.change_summary

    await session.flush()
    await session.refresh(v)

    return VersionDetailResponse(
        id=v.id,
        ontology_id=v.ontology_id,
        version=v.version,
        status=v.status,
        is_baseline=v.is_baseline,
        change_summary=v.change_summary,
        published_at=_to_iso(v.published_at),
        created_at=_to_iso(v.created_at),
        updated_at=_to_iso(v.updated_at),
        class_snapshot=v.class_snapshot or [],
        property_snapshot=v.property_snapshot or [],
        relation_snapshot=v.relation_snapshot or [],
        constraint_snapshot=v.constraint_snapshot or [],
        class_count=len(v.class_snapshot or []),
        property_count=len(v.property_snapshot or []),
        relation_count=len(v.relation_snapshot or []),
        constraint_count=len(v.constraint_snapshot or []),
    )


@router.post(
    "/{ontology_id}/versions/{version_id}/publish",
    response_model=PublishResponse,
)
async def publish_version(
    ontology_id: uuid.UUID,
    version_id: uuid.UUID,
    data: PublishVersionRequest = None,
    session: AsyncSession = Depends(get_session),
) -> PublishResponse:
    """将特定版本发布为新的 head

    行为：
    - 校验版本状态为 DRAFT
    - 将 version_record 状态置为 PUBLISHED，写入 published_at
    - 将 ontology.version 更新为该版本标签，ontology.status 置为 PUBLISHED
    """
    # 1. 找本体
    onto_result = await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )
    ontology = onto_result.scalar_one_or_none()
    if not ontology:
        raise HTTPException(status_code=404, detail="本体不存在")

    # 2. 找版本
    result = await session.execute(
        select(OntologyVersion).where(
            and_(
                OntologyVersion.id == version_id,
                OntologyVersion.ontology_id == ontology_id,
            )
        )
    )
    v = result.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")

    if v.status != OntologyVersionStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail=f"只能发布 DRAFT 状态的版本，当前状态：{v.status.value}",
        )

    # 3. 发布
    v.status = OntologyVersionStatus.PUBLISHED
    v.published_at = datetime.now(timezone.utc)

    # 4. 更新本体 head
    ontology.version = v.version
    ontology.status = OntologyStatus.PUBLISHED
    ontology.class_count = len(v.class_snapshot or [])
    ontology.property_count = len(v.property_snapshot or [])

    await session.flush()
    await session.refresh(v)

    return PublishResponse(
        version_id=v.id,
        version=v.version,
        snapshot_class_count=len(v.class_snapshot or []),
        snapshot_property_count=len(v.property_snapshot or []),
        snapshot_relation_count=len(v.relation_snapshot or []),
        snapshot_constraint_count=len(v.constraint_snapshot or []),
        published_at=v.published_at.isoformat() if v.published_at else "",
    )

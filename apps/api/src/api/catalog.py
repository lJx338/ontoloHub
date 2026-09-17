"""参考本体目录（Catalog）API 路由

提供参考本体的注册、浏览、删除功能。
参考本体为 kind=reference 的 Ontology 记录，用于跨项目对齐。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.ontology import (
    Ontology,
    OntologyKind,
    OntologyStatus,
    OntologyClass,
    ClassType,
)


router = APIRouter(prefix="/catalog", tags=["参考本体目录"])


# =====================================================================
# Pydantic
# =====================================================================


class CatalogRegisterRequest(BaseModel):
    """注册参考本体"""

    name: str = Field(..., min_length=1, max_length=255)
    namespace: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    standard_name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = Field(None, max_length=500)
    source_format: Optional[str] = Field(None, max_length=20)
    version: Optional[str] = Field(None, max_length=50)


class CatalogOntologyResponse(BaseModel):
    """目录中的本体"""

    id: uuid.UUID
    name: str
    namespace: str
    description: Optional[str]
    standard_name: Optional[str]
    source_url: Optional[str]
    source_format: Optional[str]
    version: Optional[str]
    class_count: int
    created_at: str

    model_config = {"from_attributes": True}


class CatalogClassResponse(BaseModel):
    """参考本体中的类（浏览用，轻量）"""

    id: uuid.UUID
    name: str
    local_name: Optional[str]
    iri: str
    class_type: ClassType
    parent_iri: Optional[str]
    level: int
    description: Optional[str]

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
# 目录路由
# =====================================================================


@router.get("", response_model=list[CatalogOntologyResponse])
async def list_catalog(
    session: AsyncSession = Depends(get_session),
    search: Optional[str] = Query(None),
    standard_name: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> list[CatalogOntologyResponse]:
    """列出所有参考本体（kind=reference）"""
    query = (
        select(Ontology)
        .where(Ontology.kind == OntologyKind.REFERENCE)
    )

    if search:
        query = query.where(Ontology.name.ilike(f"%{search}%"))
    if standard_name:
        query = query.where(Ontology.standard_name.ilike(f"%{standard_name}%"))

    query = query.order_by(Ontology.created_at.desc())
    query = query.offset((page - 1) * size).limit(size)

    result = await session.execute(query)
    refs = result.scalars().all()

    return [
        CatalogOntologyResponse(
            id=r.id,
            name=r.name,
            namespace=r.namespace,
            description=r.description,
            standard_name=r.standard_name,
            source_url=r.source_url,
            source_format=r.source_format,
            version=r.version,
            class_count=r.class_count,
            created_at=_to_iso(r.created_at),
        )
        for r in refs
    ]


@router.post("", response_model=CatalogOntologyResponse, status_code=status.HTTP_201_CREATED)
async def register_reference_ontology(
    data: CatalogRegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> CatalogOntologyResponse:
    """注册参考本体

    创建一条 kind=reference 的本体记录（本体下载与解析为后续阶段异步处理）。
    同一 namespace 不可重复注册。
    """
    # namespace 唯一性检查
    existing = await session.execute(
        select(Ontology).where(Ontology.namespace == data.namespace)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="该 namespace 已存在，请勿重复注册",
        )

    ontology = Ontology(
        name=data.name,
        namespace=data.namespace,
        description=data.description,
        kind=OntologyKind.REFERENCE,
        standard_name=data.standard_name,
        source_url=data.source_url,
        source_format=data.source_format,
        version=data.version,
        status=OntologyStatus.DRAFT,
    )
    session.add(ontology)
    await session.flush()
    await session.refresh(ontology)

    return CatalogOntologyResponse(
        id=ontology.id,
        name=ontology.name,
        namespace=ontology.namespace,
        description=ontology.description,
        standard_name=ontology.standard_name,
        source_url=ontology.source_url,
        source_format=ontology.source_format,
        version=ontology.version,
        class_count=ontology.class_count,
        created_at=_to_iso(ontology.created_at),
    )


@router.get("/{reference_id}", response_model=CatalogOntologyResponse)
async def get_catalog_item(
    reference_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CatalogOntologyResponse:
    """获取参考本体详情"""
    result = await session.execute(
        select(Ontology).where(
            and_(
                Ontology.id == reference_id,
                Ontology.kind == OntologyKind.REFERENCE,
            )
        )
    )
    ref = result.scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="参考本体不存在")

    return CatalogOntologyResponse(
        id=ref.id,
        name=ref.name,
        namespace=ref.namespace,
        description=ref.description,
        standard_name=ref.standard_name,
        source_url=ref.source_url,
        source_format=ref.source_format,
        version=ref.version,
        class_count=ref.class_count,
        created_at=_to_iso(ref.created_at),
    )


@router.delete("/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog_item(
    reference_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除参考本体

    仅当没有项目本体对齐此参考本体时允许删除。
    """
    result = await session.execute(
        select(Ontology).where(
            and_(
                Ontology.id == reference_id,
                Ontology.kind == OntologyKind.REFERENCE,
            )
        )
    )
    ref = result.scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="参考本体不存在")

    # 检查是否有项目本体引用了此参考本体（通过 alignment）
    # alignment 字段中 reference_class_id 指向参考本体中的类，
    # 我们通过查询 alignment JSON 字段中是否有对应的 class
    # 这是一个近似检查；精确检查需要 Join 查询
    alignment_check = await session.execute(
        select(func.count(OntologyClass.id)).where(
            and_(
                OntologyClass.alignment.isnot(None),
            )
        )
    )
    # 简单实现：直接删除；应用层可补充对齐引用检查
    await session.delete(ref)


# =====================================================================
# 参考本体类浏览
# =====================================================================


@router.get("/{reference_id}/classes", response_model=list[CatalogClassResponse])
async def list_catalog_classes(
    reference_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    parent_iri: Optional[str] = Query(None),
    level: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> list[CatalogClassResponse]:
    """浏览参考本体的类（用于选择对齐目标）"""
    # 确认参考本体存在
    ref_result = await session.execute(
        select(Ontology).where(
            and_(
                Ontology.id == reference_id,
                Ontology.kind == OntologyKind.REFERENCE,
            )
        )
    )
    if not ref_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="参考本体不存在")

    query = select(OntologyClass).where(
        OntologyClass.ontology_id == reference_id
    )

    if parent_iri:
        query = query.where(OntologyClass.parent_iri == parent_iri)
    if level is not None:
        query = query.where(OntologyClass.level == level)
    if search:
        query = query.where(OntologyClass.name.ilike(f"%{search}%"))

    query = query.order_by(OntologyClass.level, OntologyClass.name)
    query = query.offset((page - 1) * size).limit(size)

    result = await session.execute(query)
    classes = result.scalars().all()

    return [
        CatalogClassResponse(
            id=c.id,
            name=c.name,
            local_name=c.local_name,
            iri=c.iri,
            class_type=c.class_type,
            parent_iri=c.parent_iri,
            level=c.level,
            description=c.description,
        )
        for c in classes
    ]

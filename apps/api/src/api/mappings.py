"""映射 API 路由"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.mapping import (
    MappingVersion,
    IdentityMapping,
    MappingStatus,
    IdentityKeyType,
)

router = APIRouter(prefix="/mappings", tags=["映射"])


# ============ Pydantic 模型 ============

class MappingVersionCreate(BaseModel):
    version: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None


class MappingVersionResponse(BaseModel):
    id: uuid.UUID
    version: str
    status: MappingStatus
    description: Optional[str]
    total_mappings: int
    validated_mappings: int
    failed_mappings: int
    created_at: str

    model_config = {"from_attributes": True}


class IdentityMappingCreate(BaseModel):
    source_system: str = Field(..., min_length=1, max_length=100)
    source_table: Optional[str] = None
    source_field: str = Field(..., min_length=1, max_length=255)
    source_type: Optional[str] = None
    target_class_iri: str = Field(..., min_length=1, max_length=500)
    target_property_iri: Optional[str] = None
    key_type: IdentityKeyType = IdentityKeyType.CANDIDATE
    is_identity_key: bool = False
    transformation: Optional[dict] = None
    transformation_function: Optional[str] = None
    enum_mapping: Optional[dict] = None
    unit_mapping: Optional[dict] = None
    null_policy: Optional[str] = None
    rationale: Optional[str] = None


class IdentityMappingUpdate(BaseModel):
    target_class_iri: Optional[str] = None
    target_property_iri: Optional[str] = None
    key_type: Optional[IdentityKeyType] = None
    is_identity_key: Optional[bool] = None
    transformation: Optional[dict] = None
    transformation_function: Optional[str] = None
    enum_mapping: Optional[dict] = None
    unit_mapping: Optional[dict] = None
    null_policy: Optional[str] = None
    rationale: Optional[str] = None


class IdentityMappingResponse(BaseModel):
    id: uuid.UUID
    source_system: str
    source_table: Optional[str]
    source_field: str
    source_type: Optional[str]
    target_class_iri: str
    target_property_iri: Optional[str]
    key_type: IdentityKeyType
    is_identity_key: bool
    transformation_function: Optional[str]
    status: str
    validation_status: str
    confidence: float
    created_at: str

    model_config = {"from_attributes": True}


# ============ 映射版本路由 ============

@router.get("", response_model=list[MappingVersionResponse])
async def list_mapping_versions(
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(..., description="项目 ID"),
) -> list[MappingVersionResponse]:
    result = await session.execute(
        select(MappingVersion)
        .where(MappingVersion.project_id == project_id)
        .order_by(MappingVersion.created_at.desc())
    )
    versions = result.scalars().all()
    
    return [
        MappingVersionResponse(
            id=v.id,
            version=v.version,
            status=v.status,
            description=v.description,
            total_mappings=v.total_mappings,
            validated_mappings=v.validated_mappings,
            failed_mappings=v.failed_mappings,
            created_at=v.created_at.isoformat() if v.created_at else "",
        )
        for v in versions
    ]


@router.post("", response_model=MappingVersionResponse, status_code=status.HTTP_201_CREATED)
async def create_mapping_version(
    data: MappingVersionCreate,
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
) -> MappingVersionResponse:
    mapping_version = MappingVersion(
        project_id=project_id,
        version=data.version,
        description=data.description,
        status=MappingStatus.DRAFT,
    )
    
    session.add(mapping_version)
    await session.flush()
    await session.refresh(mapping_version)
    
    return MappingVersionResponse(
        id=mapping_version.id,
        version=mapping_version.version,
        status=mapping_version.status,
        description=mapping_version.description,
        total_mappings=mapping_version.total_mappings,
        validated_mappings=mapping_version.validated_mappings,
        failed_mappings=mapping_version.failed_mappings,
        created_at=mapping_version.created_at.isoformat() if mapping_version.created_at else "",
    )


@router.get("/{mapping_version_id}", response_model=MappingVersionResponse)
async def get_mapping_version(
    mapping_version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> MappingVersionResponse:
    result = await session.execute(
        select(MappingVersion).where(MappingVersion.id == mapping_version_id)
    )
    version = result.scalar_one_or_none()
    
    if not version:
        raise HTTPException(status_code=404, detail="映射版本不存在")
    
    return MappingVersionResponse(
        id=version.id,
        version=version.version,
        status=version.status,
        description=version.description,
        total_mappings=version.total_mappings,
        validated_mappings=version.validated_mappings,
        failed_mappings=version.failed_mappings,
        created_at=version.created_at.isoformat() if version.created_at else "",
    )


# ============ 身份映射路由 ============

@router.get("/{mapping_version_id}/mappings", response_model=list[IdentityMappingResponse])
async def list_identity_mappings(
    mapping_version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    target_class_iri: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
) -> list[IdentityMappingResponse]:
    query = select(IdentityMapping).where(
        IdentityMapping.mapping_version_id == mapping_version_id
    )
    
    if target_class_iri:
        query = query.where(IdentityMapping.target_class_iri == target_class_iri)
    if source_system:
        query = query.where(IdentityMapping.source_system == source_system)
    if status:
        query = query.where(IdentityMapping.status == status)
    
    query = query.order_by(IdentityMapping.source_system, IdentityMapping.source_field)
    
    result = await session.execute(query)
    mappings = result.scalars().all()
    
    return [
        IdentityMappingResponse(
            id=m.id,
            source_system=m.source_system,
            source_table=m.source_table,
            source_field=m.source_field,
            source_type=m.source_type,
            target_class_iri=m.target_class_iri,
            target_property_iri=m.target_property_iri,
            key_type=m.key_type,
            is_identity_key=m.is_identity_key,
            transformation_function=m.transformation_function,
            status=m.status,
            validation_status=m.validation_status,
            confidence=m.confidence,
            created_at=m.created_at.isoformat() if m.created_at else "",
        )
        for m in mappings
    ]


@router.post("/{mapping_version_id}/mappings", response_model=IdentityMappingResponse, status_code=status.HTTP_201_CREATED)
async def create_identity_mapping(
    mapping_version_id: uuid.UUID,
    data: IdentityMappingCreate,
    session: AsyncSession = Depends(get_session),
) -> IdentityMappingResponse:
    mapping = IdentityMapping(
        mapping_version_id=mapping_version_id,
        source_system=data.source_system,
        source_table=data.source_table,
        source_field=data.source_field,
        source_type=data.source_type,
        target_class_iri=data.target_class_iri,
        target_property_iri=data.target_property_iri,
        key_type=data.key_type,
        is_identity_key=data.is_identity_key,
        transformation=data.transformation,
        transformation_function=data.transformation_function,
        enum_mapping=data.enum_mapping,
        unit_mapping=data.unit_mapping,
        null_policy=data.null_policy,
        rationale=data.rationale,
        status="draft",
        validation_status="pending",
        confidence=0.5,
    )
    
    session.add(mapping)
    await session.flush()
    await session.refresh(mapping)
    
    return IdentityMappingResponse(
        id=mapping.id,
        source_system=mapping.source_system,
        source_table=mapping.source_table,
        source_field=mapping.source_field,
        source_type=mapping.source_type,
        target_class_iri=mapping.target_class_iri,
        target_property_iri=mapping.target_property_iri,
        key_type=mapping.key_type,
        is_identity_key=mapping.is_identity_key,
        transformation_function=mapping.transformation_function,
        status=mapping.status,
        validation_status=mapping.validation_status,
        confidence=mapping.confidence,
        created_at=mapping.created_at.isoformat() if mapping.created_at else "",
    )


@router.get("/{mapping_version_id}/mappings/{mapping_id}", response_model=IdentityMappingResponse)
async def get_identity_mapping(
    mapping_version_id: uuid.UUID,
    mapping_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> IdentityMappingResponse:
    result = await session.execute(
        select(IdentityMapping).where(
            IdentityMapping.id == mapping_id,
            IdentityMapping.mapping_version_id == mapping_version_id,
        )
    )
    mapping = result.scalar_one_or_none()
    
    if not mapping:
        raise HTTPException(status_code=404, detail="映射不存在")
    
    return IdentityMappingResponse(
        id=mapping.id,
        source_system=mapping.source_system,
        source_table=mapping.source_table,
        source_field=mapping.source_field,
        source_type=mapping.source_type,
        target_class_iri=mapping.target_class_iri,
        target_property_iri=mapping.target_property_iri,
        key_type=mapping.key_type,
        is_identity_key=mapping.is_identity_key,
        transformation_function=mapping.transformation_function,
        status=mapping.status,
        validation_status=mapping.validation_status,
        confidence=mapping.confidence,
        created_at=mapping.created_at.isoformat() if mapping.created_at else "",
    )


@router.patch("/{mapping_version_id}/mappings/{mapping_id}", response_model=IdentityMappingResponse)
async def update_identity_mapping(
    mapping_version_id: uuid.UUID,
    mapping_id: uuid.UUID,
    data: IdentityMappingUpdate,
    session: AsyncSession = Depends(get_session),
) -> IdentityMappingResponse:
    result = await session.execute(
        select(IdentityMapping).where(
            IdentityMapping.id == mapping_id,
            IdentityMapping.mapping_version_id == mapping_version_id,
        )
    )
    mapping = result.scalar_one_or_none()
    
    if not mapping:
        raise HTTPException(status_code=404, detail="映射不存在")
    
    if mapping.is_locked:
        raise HTTPException(status_code=400, detail="映射已锁定，无法修改")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(mapping, field, value)
    
    await session.flush()
    await session.refresh(mapping)
    
    return IdentityMappingResponse(
        id=mapping.id,
        source_system=mapping.source_system,
        source_table=mapping.source_table,
        source_field=mapping.source_field,
        source_type=mapping.source_type,
        target_class_iri=mapping.target_class_iri,
        target_property_iri=mapping.target_property_iri,
        key_type=mapping.key_type,
        is_identity_key=mapping.is_identity_key,
        transformation_function=mapping.transformation_function,
        status=mapping.status,
        validation_status=mapping.validation_status,
        confidence=mapping.confidence,
        created_at=mapping.created_at.isoformat() if mapping.created_at else "",
    )


@router.delete("/{mapping_version_id}/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_mapping(
    mapping_version_id: uuid.UUID,
    mapping_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(IdentityMapping).where(
            IdentityMapping.id == mapping_id,
            IdentityMapping.mapping_version_id == mapping_version_id,
        )
    )
    mapping = result.scalar_one_or_none()
    
    if not mapping:
        raise HTTPException(status_code=404, detail="映射不存在")
    
    if mapping.is_locked:
        raise HTTPException(status_code=400, detail="映射已锁定，无法删除")
    
    await session.delete(mapping)

"""Object & Link API（HIA-59）。

本体发布后的"运行时层"——围绕已部署的 ontology 版本，对业务对象实例
(Object) 与对象间关系 (Link) 提供完整的 CRUD + 查询能力。

设计要点：

- **Project 隔离** — 所有 endpoint 都先 ``require_role``，权限不足 → 404，
  与其它路由一致的侧信道策略。
- **身份键 (identity_key) upsert** — 提供 ``POST /objects/{type}`` 时若传
  ``identity_key`` 且同 project 内已存在同键对象则更新，否则创建。便于
  ETL 增量同步。
- **本体绑定校验** — 创建对象时若提供 ``ontology_class_id`` 或
  ``ontology_class_iri``，会校验对应 ontology 存在且（可选）版本状态合法。
- **链接目标校验** — 创建 Link 时 source / target 必须属于同一 project。
- **审计** — 所有写操作都通过 ``record_audit`` 写入项目级哈希链。
- **List 查询** — 支持按 ``class_iri`` / ``status`` / 全文模糊 ``q`` 过滤，
  分页与排序。

不实现：

- 不做 deep EAV 物化（InstanceData）。M0 仅支持 ``Object.data`` JSON 列
  存储即可；M1+ 切到 typed columns 时再加一层。
- 不暴露 ontology 编辑能力（已在 ``/ontologies`` 路由完成）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.object_ import (
    Object as ObjModel,
    Link as LinkModel,
    ObjectStatus,
    ObjectType,
    LinkType,
)
from src.db.ontology import Ontology, OntologyClass, OntologyVersion
from src.db.project import Project
from src.db.identity import Role
from src.api.auth import (
    CurrentPrincipal,
    get_current_user,
    record_audit,
    require_role,
)
from src.db.governance import AuditEventType


router = APIRouter(prefix="/objects", tags=["对象与链接"])


# ============================================================================
# Pydantic 模型
# ============================================================================


class ObjectCreate(BaseModel):
    """创建对象实例。

    - ``identity_key`` 若提供且同 project 内已存在同键对象，则走更新（upsert）。
    - ``data`` 字段为自由 JSON，按 ontology property 校验留给 SHACL（M1-07）。
    """

    ontology_class_id: Optional[uuid.UUID] = None
    ontology_class_iri: Optional[str] = Field(None, max_length=500)
    ontology_version_id: Optional[uuid.UUID] = None
    object_type: ObjectType = ObjectType.ENTITY

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    identity_key: Optional[str] = Field(None, max_length=500)

    data: dict[str, Any] = Field(default_factory=dict)

    source_id: Optional[uuid.UUID] = None
    mapping_version_id: Optional[uuid.UUID] = None

    status: ObjectStatus = ObjectStatus.ACTIVE
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ObjectUpdate(BaseModel):
    """部分更新对象实例。"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    status: Optional[ObjectStatus] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    object_type: Optional[ObjectType] = None


class ObjectResponse(BaseModel):
    """对象实例响应。"""

    id: uuid.UUID
    project_id: uuid.UUID
    ontology_class_id: Optional[uuid.UUID]
    ontology_class_iri: str
    ontology_version_id: Optional[uuid.UUID]
    object_type: ObjectType
    name: str
    description: Optional[str]
    identity_key: Optional[str]
    data: dict[str, Any]
    source_id: Optional[uuid.UUID]
    mapping_version_id: Optional[uuid.UUID]
    status: ObjectStatus
    confidence: float
    is_validated: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class ObjectListResponse(BaseModel):
    """对象列表（带分页）。"""

    items: list[ObjectResponse]
    total: int
    limit: int
    offset: int


class LinkCreate(BaseModel):
    """创建对象间链接。

    - ``source_id`` / ``target_id`` 都必须属于当前 project。
    - 若 ``identity_key`` 提供，同 ``(source, target, ontology_relation_iri)``
      已存在则更新（去重）。
    """

    source_id: uuid.UUID
    target_id: uuid.UUID

    link_type: LinkType = LinkType.ASSOCIATION
    ontology_relation_id: Optional[uuid.UUID] = None
    ontology_relation_iri: Optional[str] = Field(None, max_length=500)
    identity_key: Optional[str] = Field(None, max_length=500)
    properties: Optional[dict[str, Any]] = None

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class LinkUpdate(BaseModel):
    """部分更新链接。"""

    link_type: Optional[LinkType] = None
    properties: Optional[dict[str, Any]] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


class LinkResponse(BaseModel):
    """链接响应。"""

    id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    link_type: LinkType
    ontology_relation_id: Optional[uuid.UUID]
    ontology_relation_iri: Optional[str]
    identity_key: Optional[str]
    properties: Optional[dict[str, Any]]
    confidence: float
    is_validated: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class LinkListResponse(BaseModel):
    """链接列表（带分页）。"""

    items: list[LinkResponse]
    total: int
    limit: int
    offset: int


class BulkUpsertRequest(BaseModel):
    """批量 upsert 对象实例。

    ETL / 同步场景：一次请求把一批（``ObjectCreate``）灌入，按
    ``identity_key`` 决定新建或覆盖。返回成功数 / 失败数。
    """

    items: list[ObjectCreate]
    replace: bool = False  # True 时覆盖整个 data 字典；False 时浅合并


class BulkUpsertResponse(BaseModel):
    inserted: int
    updated: int
    failed: int
    errors: list[str] = Field(default_factory=list)


# ============================================================================
# 辅助函数
# ============================================================================


def _obj_to_response(o: ObjModel) -> ObjectResponse:
    return ObjectResponse(
        id=o.id,
        project_id=o.project_id,
        ontology_class_id=o.ontology_class_id,
        ontology_class_iri=o.ontology_class_iri,
        ontology_version_id=o.ontology_version_id,
        object_type=o.object_type,
        name=o.name,
        description=o.description,
        identity_key=o.identity_key,
        data=o.data or {},
        source_id=o.source_id,
        mapping_version_id=o.mapping_version_id,
        status=o.status,
        confidence=o.confidence,
        is_validated=o.is_validated,
        created_at=o.created_at.isoformat() if o.created_at else "",
        updated_at=o.updated_at.isoformat() if o.updated_at else "",
    )


def _link_to_response(l: LinkModel) -> LinkResponse:
    return LinkResponse(
        id=l.id,
        project_id=l.project_id,
        source_id=l.source_id,
        target_id=l.target_id,
        link_type=l.link_type,
        ontology_relation_id=l.ontology_relation_id,
        ontology_relation_iri=l.ontology_relation_iri,
        identity_key=l.identity_key,
        properties=l.properties,
        confidence=l.confidence,
        is_validated=l.is_validated,
        created_at=l.created_at.isoformat() if l.created_at else "",
        updated_at=l.updated_at.isoformat() if l.updated_at else "",
    )


async def _validate_ontology_class(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    class_id: Optional[uuid.UUID],
    class_iri: Optional[str],
    version_id: Optional[uuid.UUID],
) -> str:
    """校验类引用合法，返回最终使用的 class_iri（必要时由 id 反查）。

    策略：
    - 如果只给 id，查询得到对应 iri 返回；id 不存在 → 400。
    - 如果给 iri 但没给 id：
      * 若 project 可见的 ontology（project-private + reference）中能找到 →
        用该 iri（说明 binding 已声明）。
      * 若找不到 → **不报错**，按 iri 原样存储（forward-compatible：允许先
        有数据，再有 ontology；M1+ SHACL 校验时再发现 unbound iri 即可）。
    - 都不给 → 返回占位 iri "owl:Thing"，允许松散模式。
    """
    if class_id is not None:
        row = await session.execute(
            select(OntologyClass).where(
                OntologyClass.id == class_id,
            )
        )
        oc = row.scalar_one_or_none()
        if oc is None:
            raise HTTPException(
                status_code=400,
                detail=f"ontology class not found: {class_id}",
            )
        return oc.iri

    if class_iri:
        # 校验该 iri 在 project 内至少一个 ontology 中可见（published 或 draft）
        q = (
            select(func.count(OntologyClass.id))
            .join(Ontology, OntologyClass.ontology_id == Ontology.id)
            .where(OntologyClass.iri == class_iri)
        )
        # 私有 ontology 必须挂在当前 project；reference ontology 全局可见
        q = q.where(
            or_(
                Ontology.kind == "reference",
                Ontology.project_id == project_id,
            )
        )
        cnt = (await session.execute(q)).scalar_one()
        if cnt == 0:
            # 宽松模式：保留 iri 原样，不强制绑定（M0 兼容 forward ref）
            return class_iri
        return class_iri

    return "owl:Thing"


async def _ensure_object_visible(
    session: AsyncSession,
    *,
    object_id: uuid.UUID,
    project_id: uuid.UUID,
) -> ObjModel:
    """读取对象并强制 project 归属，权限错即 404（无侧信道）。"""
    row = await session.execute(
        select(ObjModel).where(
            ObjModel.id == object_id,
            ObjModel.project_id == project_id,
            ObjModel.deleted_at.is_(None),
        )
    )
    obj = row.scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="object not found")
    return obj


async def _ensure_link_visible(
    session: AsyncSession,
    *,
    link_id: uuid.UUID,
    project_id: uuid.UUID,
) -> LinkModel:
    row = await session.execute(
        select(LinkModel).where(
            LinkModel.id == link_id,
            LinkModel.project_id == project_id,
        )
    )
    link = row.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="link not found")
    return link


# ============================================================================
# Object 路由
# ============================================================================


@router.post(
    "/projects/{project_id}/objects",
    response_model=ObjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_object(
    project_id: uuid.UUID,
    payload: ObjectCreate,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ObjectResponse:
    """创建对象实例（identity_key upsert）。

    权限：EDITOR 起。
    """
    principal, _ = ctx
    # 校验 ontology class 引用合法
    class_iri = await _validate_ontology_class(
        session,
        project_id=project_id,
        class_id=payload.ontology_class_id,
        class_iri=payload.ontology_class_iri,
        version_id=payload.ontology_version_id,
    )

    # identity_key upsert
    existing = None
    if payload.identity_key:
        row = await session.execute(
            select(ObjModel).where(
                ObjModel.project_id == project_id,
                ObjModel.identity_key == payload.identity_key,
                ObjModel.deleted_at.is_(None),
            )
        )
        existing = row.scalar_one_or_none()

    if existing is not None:
        # 更新路径
        before = _obj_to_response(existing).model_dump(mode="json")
        existing.name = payload.name
        existing.description = payload.description
        existing.data = payload.data
        existing.status = payload.status
        existing.object_type = payload.object_type
        existing.confidence = payload.confidence
        if payload.ontology_class_id is not None:
            existing.ontology_class_id = payload.ontology_class_id
        existing.ontology_class_iri = class_iri
        if payload.ontology_version_id is not None:
            existing.ontology_version_id = payload.ontology_version_id
        await session.commit()
        await session.refresh(existing)
        after = _obj_to_response(existing).model_dump(mode="json")
        await record_audit(
            session,
            event_type=AuditEventType.UPDATE,
            principal=principal,
            project_id=project_id,
            target_type="object",
            target_id=str(existing.id),
            before=before,
            after=after,
        )
        await session.commit()
        return _obj_to_response(existing)

    obj = ObjModel(
        project_id=project_id,
        ontology_class_id=payload.ontology_class_id,
        ontology_class_iri=class_iri,
        ontology_version_id=payload.ontology_version_id,
        object_type=payload.object_type,
        name=payload.name,
        description=payload.description,
        identity_key=payload.identity_key,
        data=payload.data,
        source_id=payload.source_id,
        mapping_version_id=payload.mapping_version_id,
        status=payload.status,
        confidence=payload.confidence,
    )
    session.add(obj)
    await session.flush()
    await session.refresh(obj)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="object",
        target_id=str(obj.id),
        after={"id": str(obj.id), "name": obj.name},
    )
    await session.commit()
    return _obj_to_response(obj)


@router.get(
    "/projects/{project_id}/objects",
    response_model=ObjectListResponse,
)
async def list_objects(
    project_id: uuid.UUID,
    class_iri: Optional[str] = Query(None, max_length=500),
    object_type: Optional[ObjectType] = None,
    obj_status: Optional[ObjectStatus] = Query(None, alias="status"),
    q: Optional[str] = Query(None, description="对 name / description / identity_key 模糊匹配"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> ObjectListResponse:
    """列出对象实例，可按类 / 类型 / 状态 / 关键词过滤。

    权限：VIEWER 起。
    """
    principal, _ = ctx
    filters = [ObjModel.project_id == project_id, ObjModel.deleted_at.is_(None)]
    if class_iri:
        filters.append(ObjModel.ontology_class_iri == class_iri)
    if object_type is not None:
        filters.append(ObjModel.object_type == object_type)
    if obj_status is not None:
        filters.append(ObjModel.status == obj_status)
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                ObjModel.name.ilike(like),
                ObjModel.description.ilike(like),
                ObjModel.identity_key.ilike(like),
            )
        )

    base = select(ObjModel).where(and_(*filters))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = await session.execute(
        base.order_by(ObjModel.created_at.desc()).limit(limit).offset(offset)
    )
    items = [_obj_to_response(o) for o in rows.scalars().all()]
    return ObjectListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


@router.get(
    "/projects/{project_id}/objects/{object_id}",
    response_model=ObjectResponse,
)
async def get_object(
    project_id: uuid.UUID,
    object_id: uuid.UUID,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> ObjectResponse:
    """获取单个对象实例。"""
    principal, _ = ctx
    obj = await _ensure_object_visible(
        session, object_id=object_id, project_id=project_id
    )
    return _obj_to_response(obj)


@router.patch(
    "/projects/{project_id}/objects/{object_id}",
    response_model=ObjectResponse,
)
async def update_object(
    project_id: uuid.UUID,
    object_id: uuid.UUID,
    payload: ObjectUpdate,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ObjectResponse:
    """部分更新对象实例。"""
    principal, _ = ctx
    obj = await _ensure_object_visible(
        session, object_id=object_id, project_id=project_id
    )
    before = _obj_to_response(obj).model_dump(mode="json")
    if payload.name is not None:
        obj.name = payload.name
    if payload.description is not None:
        obj.description = payload.description
    if payload.data is not None:
        obj.data = payload.data
    if payload.status is not None:
        obj.status = payload.status
    if payload.confidence is not None:
        obj.confidence = payload.confidence
    if payload.object_type is not None:
        obj.object_type = payload.object_type
    await session.commit()
    await session.refresh(obj)
    after = _obj_to_response(obj).model_dump(mode="json")
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        target_type="object",
        target_id=str(obj.id),
        before=before,
        after=after,
    )
    await session.commit()
    return _obj_to_response(obj)


@router.delete(
    "/projects/{project_id}/objects/{object_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_object(
    project_id: uuid.UUID,
    object_id: uuid.UUID,
    soft: bool = Query(default=True, description="False 时真删除；True 时软删除（推荐）"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除对象实例（默认软删除）。"""
    principal, _ = ctx
    obj = await _ensure_object_visible(
        session, object_id=object_id, project_id=project_id
    )
    if soft:
        obj.deleted_at = datetime.now(timezone.utc)
    else:
        await session.delete(obj)
    await session.commit()
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        project_id=project_id,
        target_type="object",
        target_id=str(object_id),
        before=None,
        after={"hard_delete": not soft},
    )
    await session.commit()


@router.post(
    "/projects/{project_id}/objects/bulk-upsert",
    response_model=BulkUpsertResponse,
)
async def bulk_upsert_objects(
    project_id: uuid.UUID,
    payload: BulkUpsertRequest,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> BulkUpsertResponse:
    """批量 upsert 对象。ETL / 同步场景友好。

    - ``identity_key`` 决定唯一性。同一 project 内同 key 已有对象会被覆盖。
    - ``replace=True`` 时整个 ``data`` 字典被覆盖；``False`` 时浅合并（按
      key 覆盖新值，保留原有 key）。
    """
    principal, _ = ctx
    inserted = 0
    updated = 0
    failed = 0
    errors: list[str] = []

    for idx, item in enumerate(payload.items):
        try:
            class_iri = await _validate_ontology_class(
                session,
                project_id=project_id,
                class_id=item.ontology_class_id,
                class_iri=item.ontology_class_iri,
                version_id=item.ontology_version_id,
            )

            existing = None
            if item.identity_key:
                row = await session.execute(
                    select(ObjModel).where(
                        ObjModel.project_id == project_id,
                        ObjModel.identity_key == item.identity_key,
                        ObjModel.deleted_at.is_(None),
                    )
                )
                existing = row.scalar_one_or_none()

            if existing is not None:
                existing.name = item.name
                existing.description = item.description
                if payload.replace:
                    existing.data = item.data
                else:
                    merged = dict(existing.data or {})
                    merged.update(item.data or {})
                    existing.data = merged
                existing.status = item.status
                existing.object_type = item.object_type
                existing.confidence = item.confidence
                existing.ontology_class_iri = class_iri
                if item.ontology_class_id is not None:
                    existing.ontology_class_id = item.ontology_class_id
                if item.ontology_version_id is not None:
                    existing.ontology_version_id = item.ontology_version_id
                updated += 1
            else:
                obj = ObjModel(
                    project_id=project_id,
                    ontology_class_id=item.ontology_class_id,
                    ontology_class_iri=class_iri,
                    ontology_version_id=item.ontology_version_id,
                    object_type=item.object_type,
                    name=item.name,
                    description=item.description,
                    identity_key=item.identity_key,
                    data=item.data,
                    source_id=item.source_id,
                    mapping_version_id=item.mapping_version_id,
                    status=item.status,
                    confidence=item.confidence,
                )
                session.add(obj)
                inserted += 1
        except HTTPException as e:
            failed += 1
            errors.append(f"item[{idx}]: {e.detail}")
        except Exception as e:  # 防止单条失败拖垮整批
            failed += 1
            errors.append(f"item[{idx}]: {type(e).__name__}: {e}")

    await session.commit()
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        target_type="object_bulk",
        target_id=str(project_id),
        after={
            "inserted": inserted,
            "updated": updated,
            "failed": failed,
        },
    )
    await session.commit()
    return BulkUpsertResponse(
        inserted=inserted, updated=updated, failed=failed, errors=errors
    )


# ============================================================================
# Link 路由
# ============================================================================


@router.post(
    "/projects/{project_id}/links",
    response_model=LinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_link(
    project_id: uuid.UUID,
    payload: LinkCreate,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> LinkResponse:
    """创建对象间链接。

    - source / target 必须存在且属于当前 project。
    - 若 ``identity_key`` + ``ontology_relation_iri`` 已存在同对 (source,
      target) 链接，走更新。
    """
    principal, _ = ctx
    # source / target 校验（project 归属）
    src = await _ensure_object_visible(
        session, object_id=payload.source_id, project_id=project_id
    )
    tgt = await _ensure_object_visible(
        session, object_id=payload.target_id, project_id=project_id
    )

    # 重复检测：同 (source, target, ontology_relation_iri) upsert
    existing = None
    if payload.ontology_relation_iri or payload.identity_key:
        q = select(LinkModel).where(
            LinkModel.project_id == project_id,
            LinkModel.source_id == payload.source_id,
            LinkModel.target_id == payload.target_id,
        )
        if payload.ontology_relation_iri:
            q = q.where(
                LinkModel.ontology_relation_iri == payload.ontology_relation_iri
            )
        elif payload.identity_key:
            q = q.where(LinkModel.identity_key == payload.identity_key)
        row = await session.execute(q)
        existing = row.scalar_one_or_none()

    if existing is not None:
        before = _link_to_response(existing).model_dump(mode="json")
        existing.link_type = payload.link_type
        existing.properties = payload.properties
        existing.confidence = payload.confidence
        if payload.ontology_relation_id is not None:
            existing.ontology_relation_id = payload.ontology_relation_id
        if payload.ontology_relation_iri is not None:
            existing.ontology_relation_iri = payload.ontology_relation_iri
        await session.commit()
        await session.refresh(existing)
        after = _link_to_response(existing).model_dump(mode="json")
        await record_audit(
            session,
            event_type=AuditEventType.UPDATE,
            principal=principal,
            project_id=project_id,
            target_type="link",
            target_id=str(existing.id),
            before=before,
            after=after,
        )
        await session.commit()
        return _link_to_response(existing)

    link = LinkModel(
        project_id=project_id,
        source_id=src.id,
        target_id=tgt.id,
        link_type=payload.link_type,
        ontology_relation_id=payload.ontology_relation_id,
        ontology_relation_iri=payload.ontology_relation_iri,
        identity_key=payload.identity_key,
        properties=payload.properties,
        confidence=payload.confidence,
    )
    session.add(link)
    await session.flush()
    await session.refresh(link)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="link",
        target_id=str(link.id),
        after={
            "source_id": str(link.source_id),
            "target_id": str(link.target_id),
            "link_type": link.link_type.value,
        },
    )
    await session.commit()
    return _link_to_response(link)


@router.get(
    "/projects/{project_id}/links",
    response_model=LinkListResponse,
)
async def list_links(
    project_id: uuid.UUID,
    source_id: Optional[uuid.UUID] = None,
    target_id: Optional[uuid.UUID] = None,
    link_type: Optional[LinkType] = None,
    relation_iri: Optional[str] = Query(None, max_length=500),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> LinkListResponse:
    """列出链接，可按 source / target / 类型 / relation IRI 过滤。"""
    principal, _ = ctx
    filters = [LinkModel.project_id == project_id]
    if source_id is not None:
        filters.append(LinkModel.source_id == source_id)
    if target_id is not None:
        filters.append(LinkModel.target_id == target_id)
    if link_type is not None:
        filters.append(LinkModel.link_type == link_type)
    if relation_iri:
        filters.append(LinkModel.ontology_relation_iri == relation_iri)

    base = select(LinkModel).where(and_(*filters))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = await session.execute(
        base.order_by(LinkModel.created_at.desc()).limit(limit).offset(offset)
    )
    items = [_link_to_response(l) for l in rows.scalars().all()]
    return LinkListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/projects/{project_id}/links/{link_id}",
    response_model=LinkResponse,
)
async def get_link(
    project_id: uuid.UUID,
    link_id: uuid.UUID,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> LinkResponse:
    principal, _ = ctx
    link = await _ensure_link_visible(
        session, link_id=link_id, project_id=project_id
    )
    return _link_to_response(link)


@router.patch(
    "/projects/{project_id}/links/{link_id}",
    response_model=LinkResponse,
)
async def update_link(
    project_id: uuid.UUID,
    link_id: uuid.UUID,
    payload: LinkUpdate,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> LinkResponse:
    principal, _ = ctx
    link = await _ensure_link_visible(
        session, link_id=link_id, project_id=project_id
    )
    before = _link_to_response(link).model_dump(mode="json")
    if payload.link_type is not None:
        link.link_type = payload.link_type
    if payload.properties is not None:
        link.properties = payload.properties
    if payload.confidence is not None:
        link.confidence = payload.confidence
    await session.commit()
    await session.refresh(link)
    after = _link_to_response(link).model_dump(mode="json")
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project_id,
        target_type="link",
        target_id=str(link.id),
        before=before,
        after=after,
    )
    await session.commit()
    return _link_to_response(link)


@router.delete(
    "/projects/{project_id}/links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_link(
    project_id: uuid.UUID,
    link_id: uuid.UUID,
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> None:
    principal, _ = ctx
    link = await _ensure_link_visible(
        session, link_id=link_id, project_id=project_id
    )
    await session.delete(link)
    await session.commit()
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        project_id=project_id,
        target_type="link",
        target_id=str(link_id),
    )
    await session.commit()

"""项目 API 路由（HIA-51 M1-01 改造）。

变更要点：
- 列表 / 创建需要 ``get_current_user``；列表只返回调用方是成员的项目。
- 创建项目时调用方自动获得 ``OWNER`` 成员关系；并写入 ``audit_events``。
- 项目级路由使用 ``require_role(Role.X)``，项目不存在 / 不在成员里 → 404（无侧信道）。
- 删除走软删除：标记 ``deleted_at`` + 写审计。
- 用例 / 需求路由：list → VIEWER 可读；create → EDITOR 起。
- 证据路由（HIA-49 / HIA-55 / M1-02）：支持文件上传 + URL 上报 + 批量对齐。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path as PathlibPath
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Request, UploadFile, status
from fastapi import Path as Path_
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import Membership, Role
from src.db.project import Project, ProjectStatus, UseCase, Requirement
from src.db.evidence import Evidence, EvidenceType
from src.db.governance import AuditEventType
from src.api.auth import (
    CurrentPrincipal,
    coerce_diff,
    get_current_user,
    record_audit,
    require_role,
)
from src.core.config import get_settings

router = APIRouter(prefix="/projects", tags=["projects"])


# ============ Pydantic 模型 ============

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    customer_name: Optional[str] = None
    target_environment: Optional[str] = None
    owner_name: Optional[str] = None
    business_owner: Optional[str] = None
    business_owner_email: Optional[EmailStr] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[ProjectStatus] = None
    customer_name: Optional[str] = None
    target_environment: Optional[str] = None
    owner_name: Optional[str] = None
    business_owner: Optional[str] = None
    business_owner_email: Optional[EmailStr] = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    status: ProjectStatus
    customer_name: Optional[str]
    target_environment: Optional[str]
    owner_name: Optional[str]
    business_owner: Optional[str]
    business_owner_email: Optional[str]
    my_role: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class UseCaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    business_problem: Optional[str] = None
    acceptance_query: Optional[str] = None
    expected_result: Optional[str] = None
    importance: str = "medium"


class UseCaseResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    business_problem: Optional[str]
    acceptance_query: Optional[str]
    expected_result: Optional[str]
    status: str
    importance: str
    created_at: str

    model_config = {"from_attributes": True}


class RequirementCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    use_case_id: Optional[uuid.UUID] = None
    business_question: Optional[str] = None
    owner_name: Optional[str] = None
    importance: str = "medium"
    acceptance_query: Optional[str] = None


class RequirementResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    use_case_id: Optional[uuid.UUID]
    business_question: Optional[str]
    owner_name: Optional[str]
    importance: str
    status: str
    created_at: str

    model_config = {"from_attributes": True}


# ============ 证据路由 Pydantic 模型（HIA-49 / HIA-55 / M1-02）============

class EvidenceCreate(BaseModel):
    """手动上报证据（URL / 文本 / 外部来源）。"""
    evidence_type: EvidenceType = Field(..., description="证据类型")
    location: Optional[str] = Field(None, max_length=500, description="来源位置（如 URL、文件路径、字段名）")
    field_name: Optional[str] = Field(None, max_length=255, description="关联字段名")
    record_id: Optional[str] = Field(None, max_length=255, description="关联记录 ID")
    content: Optional[str] = Field(None, description="证据内容（文本片段、摘要等）")
    source_identifier: Optional[str] = Field(None, max_length=255, description="来源标识（如 URL、文件名）")
    source_url: Optional[str] = Field(None, max_length=500, description="来源 URL")
    ontology_class_iri: Optional[str] = Field(None, max_length=500, description="本体类 IRI")
    property_iri: Optional[str] = Field(None, max_length=500, description="本体属性 IRI")
    strength: str = Field("medium", description="证据强度: strong / medium / weak")
    notes: Optional[str] = Field(None, description="备注")


class EvidenceResponse(BaseModel):
    """证据响应模型。"""
    id: uuid.UUID
    evidence_type: EvidenceType
    location: Optional[str]
    field_name: Optional[str]
    record_id: Optional[str]
    content: Optional[str]
    source_identifier: Optional[str]
    source_url: Optional[str]
    ontology_class_iri: Optional[str]
    property_iri: Optional[str]
    is_confirmed: bool
    strength: str
    notes: Optional[str]
    created_by: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


class EvidenceUploadResponse(BaseModel):
    """文件上传证据响应。"""
    evidence: EvidenceResponse
    file_stored_path: Optional[str]
    content_hash: Optional[str]
    file_size: int


# ============ 项目路由 ============

@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    status: Optional[ProjectStatus] = Query(None, description="按状态筛选"),
    search: Optional[str] = Query(None, description="搜索名称"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectResponse]:
    """列出调用方是成员的项目。管理员看到所有项目。"""
    query = select(Project)
    if status:
        query = query.where(Project.status == status)
    if search:
        query = query.where(Project.name.ilike(f"%{search}%"))

    if not principal.is_admin:
        member_pids = select(Membership.project_id).where(
            Membership.user_id == principal.user.id,
            Membership.is_active.is_(True),
        )
        query = query.where(Project.id.in_(member_pids))

    query = query.offset(offset).limit(limit).order_by(Project.created_at.desc())
    result = await session.execute(query)
    projects = result.scalars().all()

    # 批量读 role（避免 N+1）
    role_map: dict[uuid.UUID, Role] = {}
    if projects and not principal.is_admin:
        ids = [p.id for p in projects]
        roles_q = await session.execute(
            select(Membership).where(
                Membership.user_id == principal.user.id,
                Membership.project_id.in_(ids),
                Membership.is_active.is_(True),
            )
        )
        for m in roles_q.scalars().all():
            try:
                role_map[m.project_id] = Role(m.role)
            except ValueError:
                pass

    return [
        ProjectResponse(
            id=p.id,
            name=p.name,
            description=p.description,
            status=p.status,
            customer_name=p.customer_name,
            target_environment=p.target_environment,
            owner_name=p.owner_name,
            business_owner=p.business_owner,
            business_owner_email=p.business_owner_email,
            my_role=(role_map.get(p.id).value if p.id in role_map else "owner" if principal.is_admin else None),
            created_at=p.created_at.isoformat() if p.created_at else "",
            updated_at=p.updated_at.isoformat() if p.updated_at else "",
        )
        for p in projects
    ]


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    request: Request,
    principal: CurrentPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    """任何已登录用户都可创建项目；创建者自动获得 OWNER 成员关系。"""
    project = Project(
        name=data.name,
        description=data.description,
        customer_name=data.customer_name,
        target_environment=data.target_environment,
        owner_name=data.owner_name or principal.user.display_name,
        business_owner=data.business_owner,
        business_owner_email=data.business_owner_email,
        status=ProjectStatus.DISCOVERY,
        created_by=principal.user.id,
        owner_id=principal.user.id,
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)

    # 创建者即 OWNER
    membership = Membership(
        user_id=principal.user.id,
        project_id=project.id,
        role=Role.OWNER.value,
        invited_by=principal.user.id,
        is_active=True,
    )
    session.add(membership)
    await session.flush()
    # flush 后 project / membership 的 server default 列（created_at、
    # updated_at）需 async SELECT 拉回；先 refresh 再传给 coerce_diff，
    # 避免在同步函数里触发 MissingGreenlet。
    await session.refresh(project)
    await session.refresh(membership)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project.id,
        target_type="project",
        target_id=str(project.id),
        target_label=project.name,
        after=coerce_diff(project),
        request=request,
    )

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        customer_name=project.customer_name,
        target_environment=project.target_environment,
        owner_name=project.owner_name,
        business_owner=project.business_owner,
        business_owner_email=project.business_owner_email,
        my_role="owner",
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    principal, role = ctx
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        customer_name=project.customer_name,
        target_environment=project.target_environment,
        owner_name=project.owner_name,
        business_owner=project.business_owner,
        business_owner_email=project.business_owner_email,
        my_role=role.value,
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    data: ProjectUpdate,
    request: Request,
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    principal, role = ctx
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description,
            status=project.status,
            customer_name=project.customer_name,
            target_environment=project.target_environment,
            owner_name=project.owner_name,
            business_owner=project.business_owner,
            business_owner_email=project.business_owner_email,
            my_role=role.value,
            created_at=project.created_at.isoformat() if project.created_at else "",
            updated_at=project.updated_at.isoformat() if project.updated_at else "",
        )

    before = coerce_diff(project)
    for field, value in update_data.items():
        setattr(project, field, value)
    await session.flush()
    # flush 后非主键列被 expire；先 refresh 再读。
    await session.refresh(project)
    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=principal,
        project_id=project.id,
        target_type="project",
        target_id=str(project.id),
        target_label=project.name,
        before=before,
        after=coerce_diff(project),
        request=request,
    )
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        customer_name=project.customer_name,
        target_environment=project.target_environment,
        owner_name=project.owner_name,
        business_owner=project.business_owner,
        business_owner_email=project.business_owner_email,
        my_role=role.value,
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    request: Request,
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删除：标记 deleted_at + 写审计（仅 OWNER）。"""
    from datetime import datetime, timezone
    principal, _ = ctx
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    before = coerce_diff(project)
    project.deleted_at = datetime.now(timezone.utc)
    project.deleted_by = principal.user.id
    await session.flush()
    # flush 后非主键列被 expire；先 refresh 再读。
    await session.refresh(project)
    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=principal,
        project_id=project.id,
        target_type="project",
        target_id=str(project.id),
        target_label=project.name,
        before=before,
        after=coerce_diff(project),
        request=request,
    )


# ============ 用例路由 ============

@router.get("/{project_id}/use-cases", response_model=list[UseCaseResponse])
async def list_use_cases(
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[UseCaseResponse]:
    result = await session.execute(
        select(UseCase)
        .where(UseCase.project_id == project_id)
        .order_by(UseCase.created_at.desc())
    )
    use_cases = result.scalars().all()
    return [
        UseCaseResponse(
            id=uc.id,
            name=uc.name,
            description=uc.description,
            business_problem=uc.business_problem,
            acceptance_query=uc.acceptance_query,
            expected_result=uc.expected_result,
            status=uc.status,
            importance=uc.importance,
            created_at=uc.created_at.isoformat() if uc.created_at else "",
        )
        for uc in use_cases
    ]


@router.post(
    "/{project_id}/use-cases",
    response_model=UseCaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_use_case(
    data: UseCaseCreate,
    request: Request,
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> UseCaseResponse:
    principal, _ = ctx
    use_case = UseCase(
        project_id=project_id,
        name=data.name,
        description=data.description,
        business_problem=data.business_problem,
        acceptance_query=data.acceptance_query,
        expected_result=data.expected_result,
        importance=data.importance,
        status="draft",
    )
    session.add(use_case)
    await session.flush()
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="use_case",
        target_id=str(use_case.id),
        target_label=use_case.name,
        after=coerce_diff(use_case),
        request=request,
    )
    await session.refresh(use_case)
    return UseCaseResponse(
        id=use_case.id,
        name=use_case.name,
        description=use_case.description,
        business_problem=use_case.business_problem,
        acceptance_query=use_case.acceptance_query,
        expected_result=use_case.expected_result,
        status=use_case.status,
        importance=use_case.importance,
        created_at=use_case.created_at.isoformat() if use_case.created_at else "",
    )


# ============ 需求路由 ============

@router.get("/{project_id}/requirements", response_model=list[RequirementResponse])
async def list_requirements(
    project_id: uuid.UUID = Path(..., description="project id"),
    use_case_id: Optional[uuid.UUID] = Query(None),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[RequirementResponse]:
    query = select(Requirement).where(Requirement.project_id == project_id)
    if use_case_id:
        query = query.where(Requirement.use_case_id == use_case_id)
    query = query.order_by(Requirement.created_at.desc())
    result = await session.execute(query)
    requirements = result.scalars().all()
    return [
        RequirementResponse(
            id=r.id,
            name=r.name,
            description=r.description,
            use_case_id=r.use_case_id,
            business_question=r.business_question,
            owner_name=r.owner_name,
            importance=r.importance,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in requirements
    ]


@router.post(
    "/{project_id}/requirements",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_requirement(
    data: RequirementCreate,
    request: Request,
    project_id: uuid.UUID = Path(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> RequirementResponse:
    principal, _ = ctx
    requirement = Requirement(
        project_id=project_id,
        name=data.name,
        description=data.description,
        use_case_id=data.use_case_id,
        business_question=data.business_question,
        owner_name=data.owner_name,
        importance=data.importance,
        acceptance_query=data.acceptance_query,
        status="open",
    )
    session.add(requirement)
    await session.flush()
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="requirement",
        target_id=str(requirement.id),
        target_label=requirement.name,
        after=coerce_diff(requirement),
        request=request,
    )
    await session.refresh(requirement)
    return RequirementResponse(
        id=requirement.id,
        name=requirement.name,
        description=requirement.description,
        use_case_id=requirement.use_case_id,
        business_question=requirement.business_question,
        owner_name=requirement.owner_name,
        importance=requirement.importance,
        status=requirement.status,
        created_at=requirement.created_at.isoformat() if requirement.created_at else "",
    )


# ============ 证据路由（HIA-49 / HIA-55 / M1-02）============

def _evidence_to_response(e: Evidence) -> EvidenceResponse:
    return EvidenceResponse(
        id=e.id,
        evidence_type=e.evidence_type,
        location=e.location,
        field_name=e.field_name,
        record_id=e.record_id,
        content=e.content,
        source_identifier=e.source_identifier,
        source_url=e.source_url,
        ontology_class_iri=e.ontology_class_iri,
        property_iri=e.property_iri,
        is_confirmed=e.is_confirmed,
        strength=e.strength,
        notes=e.notes,
        created_by=str(e.created_by) if e.created_by else None,
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


def _get_evidence_storage_path() -> PathlibPath:
    """返回证据文件存储根目录，不存在则创建。"""
    repo_root = PathlibPath(__file__).resolve().parents[3]  # apps/api/src/api → apps/api/src → apps/api → repo root
    storage = repo_root / "data" / "evidence_files"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@router.get("/{project_id}/evidences", response_model=list[EvidenceResponse])
async def list_project_evidences(
    project_id: uuid.UUID = Path_(..., description="project id"),
    source_id: Optional[uuid.UUID] = Query(None, description="按来源筛选"),
    evidence_type: Optional[EvidenceType] = Query(None, description="按类型筛选"),
    is_confirmed: Optional[bool] = Query(None, description="按确认状态筛选"),
    search: Optional[str] = Query(None, description="搜索内容"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> list[EvidenceResponse]:
    """列出项目下所有证据（HIA-55 M1-02：支持分页和筛选）。"""
    query = select(Evidence).where(Evidence.project_id == project_id)
    if source_id:
        query = query.where(Evidence.source_id == source_id)
    if evidence_type:
        query = query.where(Evidence.evidence_type == evidence_type)
    if is_confirmed is not None:
        query = query.where(Evidence.is_confirmed == is_confirmed)
    if search:
        query = query.where(Evidence.content.ilike(f"%{search}%"))

    query = query.offset(offset).limit(limit).order_by(Evidence.created_at.desc())
    result = await session.execute(query)
    return [_evidence_to_response(e) for e in result.scalars().all()]


@router.post(
    "/{project_id}/evidences",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_evidence(
    data: EvidenceCreate,
    request: Request,
    project_id: uuid.UUID = Path_(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> EvidenceResponse:
    """手动上报证据（URL / 文本片段 / 外部来源），不需要上传文件。"""
    principal, _ = ctx
    evidence = Evidence(
        project_id=project_id,
        evidence_type=data.evidence_type,
        location=data.location,
        field_name=data.field_name,
        record_id=data.record_id,
        content=data.content,
        source_identifier=data.source_identifier,
        source_url=data.source_url,
        ontology_class_iri=data.ontology_class_iri,
        property_iri=data.property_iri,
        strength=data.strength,
        notes=data.notes,
        extraction_method="manual",
        created_by=principal.user.id,
    )
    session.add(evidence)
    await session.flush()
    await session.refresh(evidence)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="evidence",
        target_id=str(evidence.id),
        target_label=data.field_name or data.source_identifier or data.content[:50] if data.content else "",
        after=coerce_diff(evidence),
        request=request,
    )
    return _evidence_to_response(evidence)


@router.post(
    "/{project_id}/evidences/upload",
    response_model=EvidenceUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_evidence_file(
    request: Request,
    file: UploadFile = File(..., description="证据文件（CSV/XLSX/JSON/Markdown/文本）"),
    project_id: uuid.UUID = Path_(..., description="project id"),
    evidence_type: EvidenceType = Query(
        EvidenceType.DOCUMENT,
        description="证据类型（默认 DOCUMENT）",
    ),
    field_name: Optional[str] = Query(None, description="关联字段名"),
    record_id: Optional[str] = Query(None, description="关联记录 ID"),
    location: Optional[str] = Query(None, description="来源位置描述"),
    notes: Optional[str] = Query(None, description="备注"),
    strength: str = Query("medium", description="证据强度"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> EvidenceUploadResponse:
    """上传证据文件（HIA-49）。

    - 存储到 ``data/evidence_files/<project_id>/<uuid>.<ext>``
    - 支持类型：.csv / .xlsx / .json / .md / .txt / .ttl / .owl / .nt
    - 文件大小上限由 ``UPLOAD_MAX_SIZE_MB`` 配置（默认 50 MB）
    - 上传后创建 ``Evidence`` 记录，``extraction_method = "file_upload"``
    - 不自动创建 Source（若需解析字段请用 ``/sources/upload``）
    """
    principal, _ = ctx

    cfg = get_settings().upload
    content = await file.read()
    file_size = len(content)

    if file_size == 0:
        raise HTTPException(status_code=400, detail="empty file")

    if file_size > cfg.max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file too large; max {cfg.max_size_mb} MB",
        )

    filename = file.filename or "evidence"
    ext = os.path.splitext(filename)[1].lower()
    allowed = set(cfg.allowed_types)
    if ext not in allowed and "*" not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"file type {ext} not allowed; allowed: {sorted(allowed)}",
        )

    content_hash = hashlib.sha256(content).hexdigest()
    file_id = uuid.uuid4()
    stored_name = f"{file_id}{ext}"
    storage = _get_evidence_storage_path() / str(project_id)
    storage.mkdir(parents=True, exist_ok=True)
    stored_path = storage / stored_name
    stored_path.write_bytes(content)

    evidence = Evidence(
        project_id=project_id,
        evidence_type=evidence_type,
        location=location or f"file:{stored_path.as_posix()}",
        field_name=field_name,
        record_id=record_id,
        content=None,  # 大文件不内嵌 content；摘要可后续解析
        source_identifier=filename,
        source_url=None,
        ontology_class_iri=None,
        property_iri=None,
        strength=strength,
        notes=notes,
        extraction_method="file_upload",
        extraction_params={
            "original_filename": filename,
            "content_hash": content_hash,
            "file_size": file_size,
            "content_type": file.content_type,
        },
        content_hash=content_hash,
        created_by=principal.user.id,
    )
    session.add(evidence)
    await session.flush()
    await session.refresh(evidence)
    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=principal,
        project_id=project_id,
        target_type="evidence",
        target_id=str(evidence.id),
        target_label=f"upload:{filename}",
        after={
            "evidence_id": str(evidence.id),
            "filename": filename,
            "file_size": file_size,
            "content_hash": content_hash,
            "stored_path": stored_path.as_posix(),
        },
        request=request,
    )
    return EvidenceUploadResponse(
        evidence=_evidence_to_response(evidence),
        file_stored_path=stored_path.as_posix(),
        content_hash=content_hash,
        file_size=file_size,
    )


@router.get("/{project_id}/evidences/{evidence_id}", response_model=EvidenceResponse)
async def get_project_evidence(
    evidence_id: uuid.UUID = Path_(..., description="evidence id"),
    project_id: uuid.UUID = Path_(..., description="project id"),
    ctx: tuple[CurrentPrincipal, Role] = Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> EvidenceResponse:
    """获取单条证据详情（HIA-55 M1-02）。"""
    result = await session.execute(
        select(Evidence).where(
            Evidence.id == evidence_id,
            Evidence.project_id == project_id,
        )
    )
    evidence = result.scalar_one_or_none()
    if evidence is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return _evidence_to_response(evidence)

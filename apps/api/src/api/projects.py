"""项目 API 路由（HIA-51 M1-01 改造）。

变更要点：
- 列表 / 创建需要 ``get_current_user``；列表只返回调用方是成员的项目。
- 创建项目时调用方自动获得 ``OWNER`` 成员关系；并写入 ``audit_events``。
- 项目级路由使用 ``require_role(Role.X)``，项目不存在 / 不在成员里 → 404（无侧信道）。
- 删除走软删除：标记 ``deleted_at`` + 写审计。
- 用例 / 需求路由：list → VIEWER 可读；create → EDITOR 起。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import Membership, Role
from src.db.project import Project, ProjectStatus, UseCase, Requirement
from src.api.auth import (
    CurrentPrincipal,
    coerce_diff,
    get_current_user,
    record_audit,
    require_role,
)
from src.db.governance import AuditEventType

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

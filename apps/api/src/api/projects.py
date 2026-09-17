"""项目 API 路由"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.project import Project, ProjectStatus, UseCase, Requirement

router = APIRouter(prefix="/projects", tags=["项目"])


# ============ Pydantic 模型 ============

class ProjectCreate(BaseModel):
    """创建项目"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    customer_name: Optional[str] = None
    target_environment: Optional[str] = None
    owner_name: Optional[str] = None
    business_owner: Optional[str] = None
    business_owner_email: Optional[str] = None


class ProjectUpdate(BaseModel):
    """更新项目"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[ProjectStatus] = None
    customer_name: Optional[str] = None
    target_environment: Optional[str] = None
    owner_name: Optional[str] = None
    business_owner: Optional[str] = None
    business_owner_email: Optional[str] = None


class ProjectResponse(BaseModel):
    """项目响应"""
    id: uuid.UUID
    name: str
    description: Optional[str]
    status: ProjectStatus
    customer_name: Optional[str]
    target_environment: Optional[str]
    owner_name: Optional[str]
    business_owner: Optional[str]
    business_owner_email: Optional[str]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class UseCaseCreate(BaseModel):
    """创建用例"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    business_problem: Optional[str] = None
    acceptance_query: Optional[str] = None
    expected_result: Optional[str] = None
    importance: str = "medium"


class UseCaseResponse(BaseModel):
    """用例响应"""
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
    """创建需求"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    use_case_id: Optional[uuid.UUID] = None
    business_question: Optional[str] = None
    owner_name: Optional[str] = None
    importance: str = "medium"
    acceptance_query: Optional[str] = None


class RequirementResponse(BaseModel):
    """需求响应"""
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
    session: AsyncSession = Depends(get_session),
    status: Optional[ProjectStatus] = Query(None, description="按状态筛选"),
    search: Optional[str] = Query(None, description="搜索名称"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ProjectResponse]:
    """列出项目"""
    query = select(Project)
    
    if status:
        query = query.where(Project.status == status)
    
    if search:
        query = query.where(Project.name.ilike(f"%{search}%"))
    
    query = query.offset(offset).limit(limit).order_by(Project.created_at.desc())
    
    result = await session.execute(query)
    projects = result.scalars().all()
    
    return [
        ProjectResponse(
            id=str(p.id),
            name=p.name,
            description=p.description,
            status=p.status,
            customer_name=p.customer_name,
            target_environment=p.target_environment,
            owner_name=p.owner_name,
            business_owner=p.business_owner,
            business_owner_email=p.business_owner_email,
            created_at=p.created_at.isoformat() if p.created_at else "",
            updated_at=p.updated_at.isoformat() if p.updated_at else "",
        )
        for p in projects
    ]


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    """创建项目"""
    project = Project(
        name=data.name,
        description=data.description,
        customer_name=data.customer_name,
        target_environment=data.target_environment,
        owner_name=data.owner_name,
        business_owner=data.business_owner,
        business_owner_email=data.business_owner_email,
        status=ProjectStatus.DISCOVERY,
    )
    
    session.add(project)
    await session.flush()
    await session.refresh(project)
    
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
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    """获取项目详情"""
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    
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
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    """更新项目"""
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    
    # 更新字段
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    
    await session.flush()
    await session.refresh(project)
    
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
        created_at=project.created_at.isoformat() if project.created_at else "",
        updated_at=project.updated_at.isoformat() if project.updated_at else "",
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除项目"""
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    
    await session.delete(project)


# ============ 用例路由 ============

@router.get("/{project_id}/use-cases", response_model=list[UseCaseResponse])
async def list_use_cases(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[UseCaseResponse]:
    """列出项目的用例"""
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


@router.post("/{project_id}/use-cases", response_model=UseCaseResponse, status_code=status.HTTP_201_CREATED)
async def create_use_case(
    project_id: uuid.UUID,
    data: UseCaseCreate,
    session: AsyncSession = Depends(get_session),
) -> UseCaseResponse:
    """创建用例"""
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
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    use_case_id: Optional[uuid.UUID] = Query(None),
) -> list[RequirementResponse]:
    """列出项目的需求"""
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


@router.post("/{project_id}/requirements", response_model=RequirementResponse, status_code=status.HTTP_201_CREATED)
async def create_requirement(
    project_id: uuid.UUID,
    data: RequirementCreate,
    session: AsyncSession = Depends(get_session),
) -> RequirementResponse:
    """创建需求"""
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

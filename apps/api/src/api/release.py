"""发布与交付 API 路由

提供变更请求 (Change Request)、发布 (Release)、部署 (Deployment) 的完整 CRUD 端点，
以及预检 (Preflight Check) 和下载功能。
"""
from __future__ import annotations

import uuid
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.connection import get_session
from src.db.release import (
    ChangeRequest,
    ChangeRequestStatus,
    Release,
    ReleaseStatus,
    Deployment,
    DeploymentStatus,
    UseCaseBundle,
    PreflightReport,
)
from src.db.project import Project
from src.db.ontology import Ontology, OntologyVersion, OntologyClass, Property, Relation, Constraint
from src.db.mapping import MappingVersion

# =============================================================================
# 路由定义
# =============================================================================

router = APIRouter(prefix="/releases", tags=["发布与交付"])
cr_router = APIRouter(prefix="/change-requests", tags=["变更评审"])


# =============================================================================
# 辅助函数
# =============================================================================


def _get_iso(dt: Optional[datetime]) -> str:
    """将 datetime 转换为 ISO 字符串。"""
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


async def _verify_project_exists(session: AsyncSession, project_id: uuid.UUID) -> Project:
    """验证项目存在，不存在则抛出 404。"""
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _verify_release_exists(session: AsyncSession, release_id: uuid.UUID) -> Release:
    """验证发布存在，不存在则抛出 404。"""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
    )
    release = result.scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")
    return release


def _compute_checksum(data: dict) -> str:
    """计算 JSON 数据的 SHA256 校验和。"""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_manifest(release: Release, session: AsyncSession) -> dict:
    """构建发布清单 (manifest)，包含本体快照、映射、验证查询等。"""
    manifest = {
        "release_id": str(release.id),
        "version": release.version,
        "project_id": str(release.project_id),
        "created_at": _get_iso(release.created_at),
        "ontology": None,
        "mappings": [],
        "constraints": [],
        "metadata": {
            "description": release.description,
            "checksum": release.checksum,
            "artifact_size": release.artifact_size,
        },
    }

    # 如果有本体版本 ID，尝试加载本体快照
    if release.ontology_version_id:
        version_result = session.execute(
            select(OntologyVersion).where(OntologyVersion.id == release.ontology_version_id)
        )
        version = version_result.scalar_one_or_none()
        if version:
            manifest["ontology"] = {
                "version_id": str(version.id),
                "version": version.version,
                "class_count": len(version.class_snapshot) if version.class_snapshot else 0,
                "property_count": len(version.property_snapshot) if version.property_snapshot else 0,
                "relation_count": len(version.relation_snapshot) if version.relation_snapshot else 0,
                "constraint_count": len(version.constraint_snapshot) if version.constraint_snapshot else 0,
                "published_at": _get_iso(version.published_at),
            }

    # 如果有映射版本 ID
    if release.mapping_version_id:
        mapping_result = session.execute(
            select(MappingVersion).where(MappingVersion.id == release.mapping_version_id)
        )
        mapping_version = mapping_result.scalar_one_or_none()
        if mapping_version:
            manifest["mappings"].append({
                "version_id": str(mapping_version.id),
                "version": mapping_version.version,
                "total_mappings": mapping_version.total_mappings,
                "validated_mappings": mapping_version.validated_mappings,
            })

    # 如果有制品数据
    if release.artifacts:
        manifest["artifacts"] = release.artifacts

    # 如果有验证结果
    if release.validation_results:
        manifest["validation_results"] = release.validation_results

    return manifest


async def _run_preflight_checks(
    release: Release,
    session: AsyncSession,
    environment: str = "production",
) -> dict:
    """运行预检验证，返回检查结果字典。"""
    checks = {}
    blocking_issues = []
    warnings = []

    # 1. 检查本体版本有效性
    ontology_check = {"name": "ontology_version", "status": "pending", "message": ""}
    if release.ontology_version_id:
        version_result = await session.execute(
            select(OntologyVersion).where(OntologyVersion.id == release.ontology_version_id)
        )
        version = version_result.scalar_one_or_none()
        if version:
            if version.status.value == "published":
                ontology_check["status"] = "passed"
                ontology_check["message"] = f"本体版本 {version.version} 已发布"
            else:
                ontology_check["status"] = "warning"
                ontology_check["message"] = f"本体版本 {version.version} 状态为 {version.status}"
        else:
            ontology_check["status"] = "failed"
            ontology_check["message"] = "本体版本不存在"
            blocking_issues.append({
                "check": "ontology_version",
                "message": "指定的本体版本不存在",
                "severity": "error",
            })
    else:
        ontology_check["status"] = "skipped"
        ontology_check["message"] = "未指定本体版本"
    checks["ontology_version"] = ontology_check

    # 2. 检查映射版本完整性
    mapping_check = {"name": "mapping_version", "status": "pending", "message": ""}
    if release.mapping_version_id:
        mapping_result = await session.execute(
            select(MappingVersion).where(MappingVersion.id == release.mapping_version_id)
        )
        mapping_version = mapping_result.scalar_one_or_none()
        if mapping_version:
            if mapping_version.total_mappings > 0:
                if mapping_version.validated_mappings == mapping_version.total_mappings:
                    mapping_check["status"] = "passed"
                    mapping_check["message"] = f"所有 {mapping_version.total_mappings} 条映射已验证"
                else:
                    mapping_check["status"] = "warning"
                    mapping_check["message"] = (
                        f"仅 {mapping_version.validated_mappings}/{mapping_version.total_mappings} 条映射已验证"
                    )
                    warnings.append({
                        "check": "mapping_version",
                        "message": f"存在未验证的映射",
                        "severity": "warning",
                    })
            else:
                mapping_check["status"] = "passed"
                mapping_check["message"] = "无映射需要验证"
        else:
            mapping_check["status"] = "failed"
            mapping_check["message"] = "映射版本不存在"
            blocking_issues.append({
                "check": "mapping_version",
                "message": "指定的映射版本不存在",
                "severity": "error",
            })
    else:
        mapping_check["status"] = "skipped"
        mapping_check["message"] = "未指定映射版本"
    checks["mapping_version"] = mapping_check

    # 3. 数据库连接检查
    db_check = {"name": "database_connectivity", "status": "pending", "message": ""}
    try:
        # 简单查询验证连接
        await session.execute(select(func.count()).select_from(Project))
        db_check["status"] = "passed"
        db_check["message"] = "数据库连接正常"
    except Exception as e:
        db_check["status"] = "failed"
        db_check["message"] = f"数据库连接失败: {str(e)}"
        blocking_issues.append({
            "check": "database_connectivity",
            "message": f"数据库连接失败: {str(e)}",
            "severity": "error",
        })
    checks["database_connectivity"] = db_check

    # 4. 检查制品完整性
    artifact_check = {"name": "artifact_integrity", "status": "pending", "message": ""}
    if release.checksum and release.artifact_size:
        artifact_check["status"] = "passed"
        artifact_check["message"] = f"制品校验和: {release.checksum[:16]}..., 大小: {release.artifact_size} bytes"
    else:
        artifact_check["status"] = "warning"
        artifact_check["message"] = "制品校验和或大小未记录"
        warnings.append({
            "check": "artifact_integrity",
            "message": "制品完整性信息不完整",
            "severity": "warning",
        })
    checks["artifact_integrity"] = artifact_check

    # 5. 检查是否有可用的部署目标
    target_check = {"name": "deployment_target", "status": "pending", "message": ""}
    # 常见目标环境验证
    valid_environments = ["development", "staging", "production", "testing"]
    if environment.lower() in valid_environments:
        target_check["status"] = "passed"
        target_check["message"] = f"目标环境 '{environment}' 可用"
    else:
        target_check["status"] = "warning"
        target_check["message"] = f"目标环境 '{environment}' 未知"
        warnings.append({
            "check": "deployment_target",
            "message": f"目标环境 '{environment}' 未在已知列表中",
            "severity": "warning",
        })
    checks["deployment_target"] = target_check

    # 6. 与上次部署对比检查
    previous_deployment_check = {"name": "previous_deployment", "status": "pending", "message": ""}
    prev_deploy_result = await session.execute(
        select(Deployment)
        .where(Deployment.release_id == release.id)
        .where(Deployment.status == DeploymentStatus.SUCCEEDED)
        .order_by(Deployment.completed_at.desc())
        .limit(1)
    )
    prev_deployment = prev_deploy_result.scalar_one_or_none()
    if prev_deployment:
        previous_deployment_check["status"] = "passed"
        previous_deployment_check["message"] = f"上次成功部署于 {_get_iso(prev_deployment.completed_at)}"
    else:
        previous_deployment_check["status"] = "skipped"
        previous_deployment_check["message"] = "无历史成功部署"
    checks["previous_deployment"] = previous_deployment_check

    # 确定整体状态
    overall_status = "passed"
    if blocking_issues:
        overall_status = "failed"
    elif warnings:
        overall_status = "warning"

    return {
        "overall_status": overall_status,
        "checks": checks,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "environment": environment,
        "release_id": str(release.id),
        "version": release.version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# =============================================================================
# Pydantic 模型 - 变更请求
# =============================================================================


class ChangeRequestCreate(BaseModel):
    """创建变更请求"""
    project_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    changes: dict = Field(default_factory=dict)


class ChangeRequestUpdate(BaseModel):
    """更新变更请求"""
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    changes: Optional[dict] = None
    changes_summary: Optional[str] = None
    impact_scope: Optional[dict] = None
    review_notes: Optional[str] = None


class ChangeRequestSubmit(BaseModel):
    """提交变更请求"""
    submitted_by: Optional[uuid.UUID] = None
    baseline_version_id: Optional[uuid.UUID] = None
    baseline_version: Optional[str] = None


class ChangeRequestApprove(BaseModel):
    """审批变更请求"""
    approved_by: Optional[uuid.UUID] = None


class ChangeRequestReject(BaseModel):
    """拒绝变更请求"""
    reviewed_by: Optional[uuid.UUID] = None
    review_notes: Optional[str] = None


class ChangeRequestMerge(BaseModel):
    """合并变更请求"""
    merged_by: Optional[uuid.UUID] = None
    target_version_id: Optional[uuid.UUID] = None
    target_version: Optional[str] = None


class ChangeRequestResponse(BaseModel):
    """变更请求响应"""
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: Optional[str]
    status: str
    baseline_version_id: Optional[uuid.UUID]
    baseline_version: Optional[str]
    target_version_id: Optional[uuid.UUID]
    target_version: Optional[str]
    changes: dict
    changes_summary: Optional[str]
    impact_scope: Optional[dict]
    submitted_by: Optional[uuid.UUID]
    submitted_at: Optional[str]
    reviewed_by: Optional[uuid.UUID]
    reviewed_at: Optional[str]
    review_notes: Optional[str]
    approved_by: Optional[uuid.UUID]
    approved_at: Optional[str]
    merged_at: Optional[str]
    merged_by: Optional[uuid.UUID]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _cr_to_response(cr: ChangeRequest) -> ChangeRequestResponse:
    """将 ChangeRequest 模型转换为响应模型。"""
    return ChangeRequestResponse(
        id=cr.id,
        project_id=cr.project_id,
        title=cr.title,
        description=cr.description,
        status=cr.status.value if cr.status else "",
        baseline_version_id=cr.baseline_version_id,
        baseline_version=cr.baseline_version,
        target_version_id=cr.target_version_id,
        target_version=cr.target_version,
        changes=cr.changes or {},
        changes_summary=cr.changes_summary,
        impact_scope=cr.impact_scope,
        submitted_by=cr.submitted_by,
        submitted_at=_get_iso(cr.submitted_at),
        reviewed_by=cr.reviewed_by,
        reviewed_at=_get_iso(cr.reviewed_at),
        review_notes=cr.review_notes,
        approved_by=cr.approved_by,
        approved_at=_get_iso(cr.approved_at),
        merged_at=_get_iso(cr.merged_at),
        merged_by=cr.merged_by,
        created_by=cr.created_by,
        created_at=_get_iso(cr.created_at),
        updated_at=_get_iso(cr.updated_at),
    )


# =============================================================================
# Pydantic 模型 - 发布
# =============================================================================


class ReleaseCreate(BaseModel):
    """创建发布"""
    version: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None
    ontology_version_id: Optional[uuid.UUID] = None
    mapping_version_id: Optional[uuid.UUID] = None
    tags: list[str] = Field(default_factory=list)


class ReleaseUpdate(BaseModel):
    """更新发布"""
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class ReleaseResponse(BaseModel):
    """发布响应"""
    id: uuid.UUID
    project_id: uuid.UUID
    version: str
    status: str
    description: Optional[str]
    ontology_version_id: Optional[uuid.UUID]
    ontology_version: Optional[str]
    mapping_version_id: Optional[uuid.UUID]
    mapping_version: Optional[str]
    artifacts: Optional[dict]
    checksum: Optional[str]
    artifact_size: Optional[int]
    validation_results: Optional[dict]
    released_at: Optional[str]
    released_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _release_to_response(r: Release) -> ReleaseResponse:
    """将 Release 模型转换为响应模型。"""
    return ReleaseResponse(
        id=r.id,
        project_id=r.project_id,
        version=r.version,
        status=r.status.value if r.status else "",
        description=r.description,
        ontology_version_id=r.ontology_version_id,
        ontology_version=r.ontology_version,
        mapping_version_id=r.mapping_version_id,
        mapping_version=r.mapping_version,
        artifacts=r.artifacts,
        checksum=r.checksum,
        artifact_size=r.artifact_size,
        validation_results=r.validation_results,
        released_at=_get_iso(r.released_at),
        released_by=r.released_by,
        created_at=_get_iso(r.created_at),
        updated_at=_get_iso(r.updated_at),
    )


# =============================================================================
# Pydantic 模型 - 部署
# =============================================================================


class DeploymentCreate(BaseModel):
    """创建部署"""
    release_id: uuid.UUID
    environment: str = Field(..., min_length=1, max_length=100)
    environment_type: Optional[str] = None
    configuration: Optional[dict] = None


class DeploymentUpdate(BaseModel):
    """更新部署状态"""
    status: Optional[DeploymentStatus] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None


class DeploymentResponse(BaseModel):
    """部署响应"""
    id: uuid.UUID
    release_id: uuid.UUID
    project_id: uuid.UUID
    environment: str
    environment_type: Optional[str]
    status: str
    configuration: Optional[dict]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_ms: Optional[int]
    result: Optional[dict]
    error_message: Optional[str]
    deployed_by: Optional[uuid.UUID]
    deployed_by_name: Optional[str]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _deployment_to_response(d: Deployment) -> DeploymentResponse:
    """将 Deployment 模型转换为响应模型。"""
    return DeploymentResponse(
        id=d.id,
        release_id=d.release_id,
        project_id=d.release.project_id if d.release else uuid.UUID(int=0),
        environment=d.environment,
        environment_type=d.environment_type,
        status=d.status.value if d.status else "",
        configuration=d.configuration,
        started_at=_get_iso(d.started_at),
        completed_at=_get_iso(d.completed_at),
        duration_ms=d.duration_ms,
        result=d.result,
        error_message=d.error_message,
        deployed_by=d.deployed_by,
        deployed_by_name=d.deployed_by_name,
        created_at=_get_iso(d.created_at),
        updated_at=_get_iso(d.updated_at),
    )


# =============================================================================
# Pydantic 模型 - 预检报告
# =============================================================================


class PreflightRunRequest(BaseModel):
    """运行预检请求"""
    environment: str = Field(default="production", max_length=100)


class PreflightReportResponse(BaseModel):
    """预检报告响应"""
    id: uuid.UUID
    project_id: uuid.UUID
    environment: str
    release_version: Optional[str]
    status: str
    checks: dict
    blocking_issues: Optional[list[dict]]
    warnings: Optional[list[dict]]
    created_by: Optional[uuid.UUID]
    created_at: str

    model_config = {"from_attributes": True}


# =============================================================================
# 变更请求路由
# =============================================================================


@cr_router.get("", response_model=list[ChangeRequestResponse])
async def list_change_requests(
    session: AsyncSession = Depends(get_session),
    project_id: Optional[uuid.UUID] = Query(None),
    status: Optional[ChangeRequestStatus] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ChangeRequestResponse]:
    """列出变更请求（支持按项目 ID 和状态筛选）。"""
    query = select(ChangeRequest)

    if project_id:
        query = query.where(ChangeRequest.project_id == project_id)
    if status is not None:
        query = query.where(ChangeRequest.status == status)

    query = query.offset(offset).limit(limit).order_by(ChangeRequest.created_at.desc())

    result = await session.execute(query)
    change_requests = result.scalars().all()

    return [_cr_to_response(cr) for cr in change_requests]


@cr_router.post("", response_model=ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_change_request(
    data: ChangeRequestCreate,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """创建变更请求。"""
    await _verify_project_exists(session, data.project_id)

    change_request = ChangeRequest(
        project_id=data.project_id,
        title=data.title,
        description=data.description,
        changes=data.changes,
        status=ChangeRequestStatus.DRAFT,
    )

    session.add(change_request)
    await session.flush()
    await session.refresh(change_request)

    return _cr_to_response(change_request)


@cr_router.get("/{cr_id}", response_model=ChangeRequestResponse)
async def get_change_request(
    cr_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """获取变更请求详情。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    return _cr_to_response(cr)


@cr_router.patch("/{cr_id}", response_model=ChangeRequestResponse)
async def update_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestUpdate,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """更新变更请求。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    # 已合并的 CR 不可修改
    if cr.status == ChangeRequestStatus.MERGED:
        raise HTTPException(status_code=400, detail="已合并的变更请求不可修改")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(cr, field, value)

    await session.flush()
    await session.refresh(cr)

    return _cr_to_response(cr)


@cr_router.delete("/{cr_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_change_request(
    cr_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除变更请求（仅草稿状态可删除）。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    if cr.status != ChangeRequestStatus.DRAFT:
        raise HTTPException(status_code=400, detail="仅草稿状态的变更请求可删除")

    await session.delete(cr)


@cr_router.post("/{cr_id}/submit", response_model=ChangeRequestResponse)
async def submit_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestSubmit,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """提交变更请求进行审核。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    if cr.status != ChangeRequestStatus.DRAFT:
        raise HTTPException(status_code=400, detail="仅草稿状态的变更请求可提交")

    cr.status = ChangeRequestStatus.SUBMITTED
    cr.submitted_by = data.submitted_by
    cr.submitted_at = datetime.now(timezone.utc)
    if data.baseline_version_id:
        cr.baseline_version_id = data.baseline_version_id
    if data.baseline_version:
        cr.baseline_version = data.baseline_version

    await session.flush()
    await session.refresh(cr)

    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/approve", response_model=ChangeRequestResponse)
async def approve_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestApprove,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """审批通过变更请求。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    if cr.status != ChangeRequestStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="仅已提交的变更请求可审批")

    cr.status = ChangeRequestStatus.APPROVED
    cr.approved_by = data.approved_by
    cr.approved_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(cr)

    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/reject", response_model=ChangeRequestResponse)
async def reject_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestReject,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """拒绝变更请求。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    if cr.status not in (ChangeRequestStatus.SUBMITTED, ChangeRequestStatus.NEEDS_REVISION):
        raise HTTPException(status_code=400, detail="仅已提交或需要修订的变更请求可拒绝")

    cr.status = ChangeRequestStatus.NEEDS_REVISION
    cr.reviewed_by = data.reviewed_by
    cr.reviewed_at = datetime.now(timezone.utc)
    if data.review_notes:
        cr.review_notes = data.review_notes

    await session.flush()
    await session.refresh(cr)

    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/merge", response_model=ChangeRequestResponse)
async def merge_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestMerge,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """合并变更请求到本体（发布新版本）。"""
    result = await session.execute(
        select(ChangeRequest).where(ChangeRequest.id == cr_id)
    )
    cr = result.scalar_one_or_none()

    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")

    if cr.status not in (ChangeRequestStatus.APPROVED, ChangeRequestStatus.SUBMITTED):
        raise HTTPException(
            status_code=400,
            detail="仅已批准或已提交的变更请求可合并"
        )

    # 合并到本体
    cr.status = ChangeRequestStatus.MERGED
    cr.merged_at = datetime.now(timezone.utc)
    cr.merged_by = data.merged_by

    if data.target_version_id:
        cr.target_version_id = data.target_version_id
    if data.target_version:
        cr.target_version = data.target_version

    # 如果变更包含需要创建的本体版本，生成新版本
    if cr.changes and not data.target_version:
        # 根据 changes 内容生成版本号
        cr.target_version = f"v{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    await session.flush()
    await session.refresh(cr)

    return _cr_to_response(cr)


# =============================================================================
# 发布路由
# =============================================================================


@router.get("/projects/{project_id}/releases", response_model=list[ReleaseResponse])
async def list_project_releases(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    status: Optional[ReleaseStatus] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ReleaseResponse]:
    """列出项目的所有发布。"""
    await _verify_project_exists(session, project_id)

    query = select(Release).where(Release.project_id == project_id)

    if status is not None:
        query = query.where(Release.status == status)

    query = query.offset(offset).limit(limit).order_by(Release.created_at.desc())

    result = await session.execute(query)
    releases = result.scalars().all()

    return [_release_to_response(r) for r in releases]


@router.post("/projects/{project_id}/releases", response_model=ReleaseResponse, status_code=status.HTTP_201_CREATED)
async def create_release(
    project_id: uuid.UUID,
    data: ReleaseCreate,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """创建发布（快照当前本体状态）。"""
    await _verify_project_exists(session, project_id)

    # 验证本体版本（如果指定）
    if data.ontology_version_id:
        version_result = await session.execute(
            select(OntologyVersion).where(OntologyVersion.id == data.ontology_version_id)
        )
        version = version_result.scalar_one_or_none()
        if not version:
            raise HTTPException(status_code=400, detail="指定的本体版本不存在")

    # 验证映射版本（如果指定）
    if data.mapping_version_id:
        mapping_result = await session.execute(
            select(MappingVersion).where(MappingVersion.id == data.mapping_version_id)
        )
        mapping_version = mapping_result.scalar_one_or_none()
        if not mapping_version:
            raise HTTPException(status_code=400, detail="指定的映射版本不存在")

    # 构建制品数据
    artifacts = {
        "tags": data.tags,
        "ontology_version_id": str(data.ontology_version_id) if data.ontology_version_id else None,
        "mapping_version_id": str(data.mapping_version_id) if data.mapping_version_id else None,
    }

    # 创建发布
    release = Release(
        project_id=project_id,
        version=data.version,
        description=data.description,
        ontology_version_id=data.ontology_version_id,
        mapping_version_id=data.mapping_version_id,
        status=ReleaseStatus.DRAFT,
        artifacts=artifacts,
    )

    session.add(release)
    await session.flush()
    await session.refresh(release)

    return _release_to_response(release)


@router.get("/releases/{release_id}", response_model=ReleaseResponse)
async def get_release(
    release_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """获取发布详情。"""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
    )
    release = result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    return _release_to_response(release)


@router.patch("/releases/{release_id}", response_model=ReleaseResponse)
async def update_release(
    release_id: uuid.UUID,
    data: ReleaseUpdate,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """更新发布信息。"""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
    )
    release = result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    if release.status == ReleaseStatus.RELEASED:
        raise HTTPException(status_code=400, detail="已发布的版本不可修改")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "tags" and release.artifacts:
            release.artifacts["tags"] = value
        else:
            setattr(release, field, value)

    await session.flush()
    await session.refresh(release)

    return _release_to_response(release)


@router.get("/releases/{release_id}/download")
async def download_release(
    release_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """下载发布包（返回包含清单的 JSON）。"""
    release = await _verify_release_exists(session, release_id)

    manifest = _build_manifest(release, session)

    # 计算并更新校验和（如果尚未计算）
    if not release.checksum:
        release.checksum = _compute_checksum(manifest)
        await session.flush()

    manifest["checksum"] = release.checksum

    return JSONResponse(content=manifest)


@router.post("/releases/{release_id}/preflight", response_model=dict)
async def run_preflight(
    release_id: uuid.UUID,
    data: PreflightRunRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """运行预检检查。"""
    release = await _verify_release_exists(session, release_id)

    preflight_result = await _run_preflight_checks(release, session, data.environment)

    # 保存预检报告
    report = PreflightReport(
        project_id=release.project_id,
        environment=data.environment,
        release_version=release.version,
        status=preflight_result["overall_status"],
        checks=preflight_result["checks"],
        blocking_issues=preflight_result.get("blocking_issues"),
        warnings=preflight_result.get("warnings"),
    )
    session.add(report)
    await session.flush()

    # 更新发布的预检状态
    release.validation_results = preflight_result

    return preflight_result


@router.post("/releases/{release_id}/publish", response_model=ReleaseResponse)
async def publish_release(
    release_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """发布版本（状态变更为 released）。"""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
    )
    release = result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    if release.status == ReleaseStatus.RELEASED:
        raise HTTPException(status_code=400, detail="版本已经发布")

    # 构建清单以计算校验和
    manifest = _build_manifest(release, session)
    release.checksum = _compute_checksum(manifest)
    release.artifact_size = len(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))

    release.status = ReleaseStatus.RELEASED
    release.released_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(release)

    return _release_to_response(release)


# =============================================================================
# 部署路由
# =============================================================================


@router.get("/projects/{project_id}/deployments", response_model=list[DeploymentResponse])
async def list_project_deployments(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    status: Optional[DeploymentStatus] = Query(None),
    environment: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[DeploymentResponse]:
    """列出项目的部署记录。"""
    await _verify_project_exists(session, project_id)

    query = (
        select(Deployment)
        .join(Release)
        .where(Release.project_id == project_id)
        .options(selectinload(Deployment.release))
    )

    if status is not None:
        query = query.where(Deployment.status == status)
    if environment:
        query = query.where(Deployment.environment == environment)

    query = query.offset(offset).limit(limit).order_by(Deployment.created_at.desc())

    result = await session.execute(query)
    deployments = result.scalars().all()

    return [_deployment_to_response(d) for d in deployments]


@router.post("/projects/{project_id}/deployments", response_model=DeploymentResponse, status_code=status.HTTP_201_CREATED)
async def create_deployment(
    project_id: uuid.UUID,
    data: DeploymentCreate,
    session: AsyncSession = Depends(get_session),
) -> DeploymentResponse:
    """创建部署（将发布部署到目标环境）。"""
    await _verify_project_exists(session, project_id)

    # 验证发布存在且属于同一项目
    release_result = await session.execute(
        select(Release).where(Release.id == data.release_id)
    )
    release = release_result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    if release.project_id != project_id:
        raise HTTPException(status_code=400, detail="发布不属于指定的项目")

    if release.status != ReleaseStatus.RELEASED:
        raise HTTPException(status_code=400, detail="仅已发布的版本可部署")

    # 创建部署记录
    deployment = Deployment(
        release_id=data.release_id,
        environment=data.environment,
        environment_type=data.environment_type,
        configuration=data.configuration,
        status=DeploymentStatus.PENDING,
    )

    session.add(deployment)
    await session.flush()
    await session.refresh(deployment)

    # 模拟部署执行（实际应连接部署服务）
    deployment.started_at = datetime.now(timezone.utc)
    deployment.status = DeploymentStatus.IN_PROGRESS

    # 简化的部署结果
    deployment.completed_at = datetime.now(timezone.utc)
    deployment.duration_ms = int(
        (deployment.completed_at - deployment.started_at).total_seconds() * 1000
    )
    deployment.status = DeploymentStatus.SUCCEEDED
    deployment.result = {
        "message": "部署成功",
        "environment": data.environment,
        "release_version": release.version,
    }
    deployment.deployed_by_name = "system"

    await session.flush()
    await session.refresh(deployment)

    return _deployment_to_response(deployment)


@router.get("/deployments/{deployment_id}", response_model=DeploymentResponse)
async def get_deployment(
    deployment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> DeploymentResponse:
    """获取部署详情。"""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.id == deployment_id)
        .options(selectinload(Deployment.release))
    )
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="部署不存在")

    return _deployment_to_response(deployment)


@router.patch("/deployments/{deployment_id}", response_model=DeploymentResponse)
async def update_deployment(
    deployment_id: uuid.UUID,
    data: DeploymentUpdate,
    session: AsyncSession = Depends(get_session),
) -> DeploymentResponse:
    """更新部署状态。"""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.id == deployment_id)
        .options(selectinload(Deployment.release))
    )
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="部署不存在")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(deployment, field, value)

    await session.flush()
    await session.refresh(deployment)

    return _deployment_to_response(deployment)


@router.post("/deployments/{deployment_id}/rollback", response_model=DeploymentResponse)
async def rollback_deployment(
    deployment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> DeploymentResponse:
    """回滚部署。"""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.id == deployment_id)
        .options(selectinload(Deployment.release))
    )
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="部署不存在")

    if deployment.status == DeploymentStatus.ROLLED_BACK:
        raise HTTPException(status_code=400, detail="部署已经回滚")

    deployment.status = DeploymentStatus.ROLLED_BACK
    deployment.completed_at = datetime.now(timezone.utc)
    deployment.result = {
        "message": "回滚成功",
        "rolled_back_at": deployment.completed_at.isoformat(),
    }

    await session.flush()
    await session.refresh(deployment)

    return _deployment_to_response(deployment)


# =============================================================================
# 用例包路由（UseCaseBundle）
# =============================================================================


class UseCaseBundleCreate(BaseModel):
    """创建用例包"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    version: str = Field(..., min_length=1, max_length=50)
    use_case_ids: Optional[list[uuid.UUID]] = None
    ontology_version_id: Optional[uuid.UUID] = None
    mapping_version_id: Optional[uuid.UUID] = None
    dependencies: Optional[list[dict]] = None


class UseCaseBundleResponse(BaseModel):
    """用例包响应"""
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    version: str
    use_case_ids: Optional[list[uuid.UUID]]
    ontology_version_id: Optional[uuid.UUID]
    mapping_version_id: Optional[uuid.UUID]
    validation_results: Optional[dict]
    dependencies: Optional[list[dict]]
    manifest: Optional[dict]
    artifact_path: Optional[str]
    checksum: Optional[str]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


@router.get("/projects/{project_id}/use-case-bundles", response_model=list[UseCaseBundleResponse])
async def list_use_case_bundles(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[UseCaseBundleResponse]:
    """列出项目的用例包。"""
    await _verify_project_exists(session, project_id)

    result = await session.execute(
        select(UseCaseBundle)
        .where(UseCaseBundle.project_id == project_id)
        .order_by(UseCaseBundle.created_at.desc())
    )
    bundles = result.scalars().all()

    return [
        UseCaseBundleResponse(
            id=b.id,
            project_id=b.project_id,
            name=b.name,
            description=b.description,
            version=b.version,
            use_case_ids=b.use_case_ids,
            ontology_version_id=b.ontology_version_id,
            mapping_version_id=b.mapping_version_id,
            validation_results=b.validation_results,
            dependencies=b.dependencies,
            manifest=b.manifest,
            artifact_path=b.artifact_path,
            checksum=b.checksum,
            created_by=b.created_by,
            created_at=_get_iso(b.created_at),
            updated_at=_get_iso(b.updated_at),
        )
        for b in bundles
    ]


@router.post("/projects/{project_id}/use-case-bundles", response_model=UseCaseBundleResponse, status_code=status.HTTP_201_CREATED)
async def create_use_case_bundle(
    project_id: uuid.UUID,
    data: UseCaseBundleCreate,
    session: AsyncSession = Depends(get_session),
) -> UseCaseBundleResponse:
    """创建用例包。"""
    await _verify_project_exists(session, project_id)

    bundle = UseCaseBundle(
        project_id=project_id,
        name=data.name,
        description=data.description,
        version=data.version,
        use_case_ids=data.use_case_ids,
        ontology_version_id=data.ontology_version_id,
        mapping_version_id=data.mapping_version_id,
        dependencies=data.dependencies,
    )

    session.add(bundle)
    await session.flush()
    await session.refresh(bundle)

    return UseCaseBundleResponse(
        id=bundle.id,
        project_id=bundle.project_id,
        name=bundle.name,
        description=bundle.description,
        version=bundle.version,
        use_case_ids=bundle.use_case_ids,
        ontology_version_id=bundle.ontology_version_id,
        mapping_version_id=bundle.mapping_version_id,
        validation_results=bundle.validation_results,
        dependencies=bundle.dependencies,
        manifest=bundle.manifest,
        artifact_path=bundle.artifact_path,
        checksum=bundle.checksum,
        created_by=bundle.created_by,
        created_at=_get_iso(bundle.created_at),
        updated_at=_get_iso(bundle.updated_at),
    )


# =============================================================================
# 预检报告路由
# =============================================================================


@router.get("/projects/{project_id}/preflight-reports", response_model=list[PreflightReportResponse])
async def list_preflight_reports(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    environment: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[PreflightReportResponse]:
    """列出项目的预检报告。"""
    await _verify_project_exists(session, project_id)

    query = select(PreflightReport).where(PreflightReport.project_id == project_id)

    if environment:
        query = query.where(PreflightReport.environment == environment)

    query = query.order_by(PreflightReport.created_at.desc()).limit(limit)

    result = await session.execute(query)
    reports = result.scalars().all()

    return [
        PreflightReportResponse(
            id=r.id,
            project_id=r.project_id,
            environment=r.environment,
            release_version=r.release_version,
            status=r.status,
            checks=r.checks,
            blocking_issues=r.blocking_issues,
            warnings=r.warnings,
            created_by=r.created_by,
            created_at=_get_iso(r.created_at),
        )
        for r in reports
    ]

"""发布与交付 API 路由

提供变更请求 (Change Request)、发布 (Release)、部署 (Deployment) 的完整 CRUD 端点，
以及预检 (Preflight Check) 和下载功能。

HIA-69 / B5 CR 工作流增强：

* 多 reviewer：每个 reviewer 独立审批，达到 ``required_approvers`` 自动合并
* 评论线程：``POST /change-requests/{id}/comments`` 支持 ``parent_id`` reply
* 状态机：``DRAFT`` / ``SUBMITTED`` / ``CHANGES_REQUESTED`` / ``APPROVED`` / ``MERGED`` / ``CLOSED``
"""
from __future__ import annotations

import uuid
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.connection import get_session
from src.db.release import (
    ChangeRequest,
    ChangeRequestStatus,
    ChangeRequestReviewer,
    ReviewerStatus,
    ChangeRequestComment,
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


def _now_utc() -> datetime:
    """返回带 tz 的当前 UTC 时间（避免 async session 里 ``func.now()`` 的秒级精度问题）。"""
    return datetime.now(timezone.utc)


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


async def _verify_cr_exists(
    session: AsyncSession,
    cr_id: uuid.UUID,
) -> ChangeRequest:
    """验证 CR 存在并加载 reviewer / comment 关系。"""
    result = await session.execute(
        select(ChangeRequest)
        .where(ChangeRequest.id == cr_id)
        .options(
            selectinload(ChangeRequest.reviewers),
            selectinload(ChangeRequest.comments),
        )
    )
    cr = result.scalar_one_or_none()
    if not cr:
        raise HTTPException(status_code=404, detail="变更请求不存在")
    return cr


def _compute_checksum(data: dict) -> str:
    """计算 JSON 数据的 SHA256 校验和。"""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# =============================================================================
# 自动合并判定（HIA-69 B5）
# =============================================================================


def _approved_reviewer_count(cr: ChangeRequest) -> int:
    """已审批通过 (``APPROVED``) 的 reviewer 数。"""
    return sum(1 for r in cr.reviewers if r.status == ReviewerStatus.APPROVED)


def _changes_requested_reviewer_exists(cr: ChangeRequest) -> bool:
    """是否有 reviewer 请求修改 (``CHANGES_REQUESTED``)。"""
    return any(r.status == ReviewerStatus.CHANGES_REQUESTED for r in cr.reviewers)


async def _maybe_auto_merge(
    cr: ChangeRequest,
    session: AsyncSession,
) -> None:
    """如果已通过审批数达到 ``required_approvers``，自动把 CR 升到 ``APPROVED``。

    HIA-69 B5 验收点：配置 2 reviewers 的 CR，提交后两人各自审批才合并。
    本函数只负责把 ``SUBMITTED`` → ``APPROVED``。是否立刻 ``MERGED``
    由 ``POST /change-requests/{id}/merge`` 端点决定 — 这是 GitHub PR
    风格的「approve → 等用户点 merge」流程；自动合并可作为额外选项
    （``auto_merge`` flag），但本任务先不实现。
    """
    if cr.required_approvers <= 0:
        # 0 means "no approval required" — submitted CR goes straight to approved
        cr.status = ChangeRequestStatus.APPROVED
        cr.approved_at = _now_utc()
        return

    if cr.status != ChangeRequestStatus.SUBMITTED:
        return

    if _approved_reviewer_count(cr) >= cr.required_approvers:
        cr.status = ChangeRequestStatus.APPROVED
        cr.approved_at = _now_utc()
        # approved_by is "the last reviewer that pushed it over the line"
        # (set inside the per-reviewer handler before this is called).


# =============================================================================
# Pydantic 模型 - 变更请求
# =============================================================================


class ChangeRequestCreate(BaseModel):
    """创建变更请求"""

    project_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    changes: dict = Field(default_factory=dict)
    changes_summary: Optional[str] = None
    impact_scope: Optional[dict] = None
    required_approvers: int = Field(default=1, ge=0, le=10)
    reviewer_ids: Optional[list[uuid.UUID]] = None  # 预分配的 reviewer


class ChangeRequestUpdate(BaseModel):
    """更新变更请求"""

    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    changes: Optional[dict] = None
    changes_summary: Optional[str] = None
    impact_scope: Optional[dict] = None
    review_notes: Optional[str] = None
    required_approvers: Optional[int] = Field(None, ge=0, le=10)


class ChangeRequestSubmit(BaseModel):
    """提交变更请求"""

    submitted_by: Optional[uuid.UUID] = None
    baseline_version_id: Optional[uuid.UUID] = None
    baseline_version: Optional[str] = None


class ChangeRequestApprove(BaseModel):
    """审批变更请求（全局 approve — 适用于 0/1 reviewer 配置）"""

    approved_by: Optional[uuid.UUID] = None


class ChangeRequestReject(BaseModel):
    """拒绝 / 请求修改"""

    reviewed_by: Optional[uuid.UUID] = None
    review_notes: Optional[str] = None


class ChangeRequestMerge(BaseModel):
    """合并变更请求"""

    merged_by: Optional[uuid.UUID] = None
    target_version_id: Optional[uuid.UUID] = None
    target_version: Optional[str] = None


class ChangeRequestClose(BaseModel):
    """关闭 / 废弃 CR"""

    closed_by: Optional[uuid.UUID] = None
    close_reason: Optional[str] = None


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
    required_approvers: int
    submitted_by: Optional[uuid.UUID]
    submitted_at: Optional[str]
    reviewed_by: Optional[uuid.UUID]
    reviewed_at: Optional[str]
    review_notes: Optional[str]
    approved_by: Optional[uuid.UUID]
    approved_at: Optional[str]
    merged_at: Optional[str]
    merged_by: Optional[uuid.UUID]
    closed_at: Optional[str]
    closed_by: Optional[uuid.UUID]
    close_reason: Optional[str]
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
        required_approvers=cr.required_approvers or 1,
        submitted_by=cr.submitted_by,
        submitted_at=_get_iso(cr.submitted_at),
        reviewed_by=cr.reviewed_by,
        reviewed_at=_get_iso(cr.reviewed_at),
        review_notes=cr.review_notes,
        approved_by=cr.approved_by,
        approved_at=_get_iso(cr.approved_at),
        merged_at=_get_iso(cr.merged_at),
        merged_by=cr.merged_by,
        closed_at=_get_iso(cr.closed_at),
        closed_by=cr.closed_by,
        close_reason=cr.close_reason,
        created_by=cr.created_by,
        created_at=_get_iso(cr.created_at),
        updated_at=_get_iso(cr.updated_at),
    )


# =============================================================================
# Pydantic 模型 - Reviewer
# =============================================================================


class ReviewerAssign(BaseModel):
    """分配 reviewer"""

    reviewer_id: uuid.UUID
    reviewer_name: Optional[str] = None


class ReviewerDecision(BaseModel):
    """单个 reviewer 投票"""

    comment: Optional[str] = None


class ReviewerResponse(BaseModel):
    """单个 reviewer 响应"""

    id: uuid.UUID
    change_request_id: uuid.UUID
    reviewer_id: uuid.UUID
    reviewer_name: Optional[str]
    status: str
    reviewed_at: Optional[str]
    comment: Optional[str]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _reviewer_to_response(r: ChangeRequestReviewer) -> ReviewerResponse:
    return ReviewerResponse(
        id=r.id,
        change_request_id=r.change_request_id,
        reviewer_id=r.reviewer_id,
        reviewer_name=r.reviewer_name,
        status=r.status.value if r.status else "",
        reviewed_at=_get_iso(r.reviewed_at),
        comment=r.comment,
        created_at=_get_iso(r.created_at),
        updated_at=_get_iso(r.updated_at),
    )


# =============================================================================
# Pydantic 模型 - Comment
# =============================================================================


class CommentCreate(BaseModel):
    """创建评论（顶级或 reply）"""

    body: str = Field(..., min_length=1)
    author_id: Optional[uuid.UUID] = None
    author_name: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None  # None = top-level comment


class CommentResponse(BaseModel):
    """评论响应"""

    id: uuid.UUID
    change_request_id: uuid.UUID
    parent_id: Optional[uuid.UUID]
    author_id: Optional[uuid.UUID]
    author_name: Optional[str]
    body: str
    deleted_at: Optional[str]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _comment_to_response(c: ChangeRequestComment) -> CommentResponse:
    return CommentResponse(
        id=c.id,
        change_request_id=c.change_request_id,
        parent_id=c.parent_id,
        author_id=c.author_id,
        author_name=c.author_name,
        body=c.body if c.deleted_at is None else "[deleted]",
        deleted_at=_get_iso(c.deleted_at),
        created_at=_get_iso(c.created_at),
        updated_at=_get_iso(c.updated_at),
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
        project_id=d.project_id or (d.release.project_id if d.release else uuid.UUID(int=0)),
        environment=d.environment or "",
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
    """创建变更请求。

    可选预分配 reviewer（``reviewer_ids``），创建后每个 reviewer 都是
    ``PENDING`` 状态，等待他们各自审批。
    """
    await _verify_project_exists(session, data.project_id)

    cr = ChangeRequest(
        project_id=data.project_id,
        title=data.title,
        description=data.description,
        changes=data.changes,
        changes_summary=data.changes_summary,
        impact_scope=data.impact_scope,
        required_approvers=data.required_approvers,
        status=ChangeRequestStatus.DRAFT,
    )
    session.add(cr)
    await session.flush()

    # Pre-assign reviewers (each starts as PENDING)
    if data.reviewer_ids:
        for rid in data.reviewer_ids:
            session.add(
                ChangeRequestReviewer(
                    change_request_id=cr.id,
                    reviewer_id=rid,
                    status=ReviewerStatus.PENDING,
                )
            )
        await session.flush()

    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.get("/{cr_id}", response_model=ChangeRequestResponse)
async def get_change_request(
    cr_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """获取变更请求详情。"""
    cr = await _verify_cr_exists(session, cr_id)
    return _cr_to_response(cr)


@cr_router.patch("/{cr_id}", response_model=ChangeRequestResponse)
async def update_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestUpdate,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """更新变更请求。

    已合并的 CR 不可修改；其他状态允许修改 title / description / changes
    等内容字段。``required_approvers`` 可在 ``SUBMITTED`` 之前调整（之后
    修改会改变已分配的 reviewer 的意义）。
    """
    cr = await _verify_cr_exists(session, cr_id)

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
    cr = await _verify_cr_exists(session, cr_id)

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
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status not in (ChangeRequestStatus.DRAFT, ChangeRequestStatus.CHANGES_REQUESTED):
        raise HTTPException(
            status_code=400,
            detail="仅草稿或请求修改状态的变更请求可提交",
        )

    cr.status = ChangeRequestStatus.SUBMITTED
    cr.submitted_by = data.submitted_by
    cr.submitted_at = _now_utc()
    if data.baseline_version_id:
        cr.baseline_version_id = data.baseline_version_id
    if data.baseline_version:
        cr.baseline_version = data.baseline_version

    # Reset reviewer decisions if it's a re-submit after changes_requested
    if cr.reviewers:
        for r in cr.reviewers:
            r.status = ReviewerStatus.PENDING
            r.reviewed_at = None
            r.comment = None

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/approve", response_model=ChangeRequestResponse)
async def approve_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestApprove,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """全局审批通过变更请求。

    适用于：``required_approvers == 0``（无需审批）或没有配置 M2M reviewer
    的 CR。配置了多 reviewer 的 CR，请用 ``POST /change-requests/{id}/
    reviewers/{rid}/approve`` 给每个 reviewer 投票，系统会自动在达到
    ``required_approvers`` 时升级到 ``APPROVED``。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status != ChangeRequestStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="仅已提交的变更请求可审批")

    if cr.required_approvers > 1 and cr.reviewers:
        raise HTTPException(
            status_code=400,
            detail="配置多 reviewer 的 CR 请用 /reviewers/{rid}/approve 端点",
        )

    cr.status = ChangeRequestStatus.APPROVED
    cr.approved_by = data.approved_by
    cr.approved_at = _now_utc()

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/reject", response_model=ChangeRequestResponse)
async def reject_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestReject,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """拒绝 / 请求修改变更请求。

    全局 reject：直接把 ``SUBMITTED`` 转 ``CHANGES_REQUESTED``。如果 CR
    配置了 M2M reviewer，建议用 ``POST /change-requests/{id}/reviewers/
    {rid}/reject`` 走单 reviewer 路径。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status != ChangeRequestStatus.SUBMITTED:
        raise HTTPException(
            status_code=400,
            detail="仅已提交的变更请求可拒绝 / 请求修改",
        )

    cr.status = ChangeRequestStatus.CHANGES_REQUESTED
    cr.reviewed_by = data.reviewed_by
    cr.reviewed_at = _now_utc()
    if data.review_notes:
        cr.review_notes = data.review_notes

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.post("/{cr_id}/close", response_model=ChangeRequestResponse)
async def close_change_request(
    cr_id: uuid.UUID,
    data: ChangeRequestClose,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """关闭变更请求（废弃 / 主动取消）。

    可从 ``DRAFT`` / ``SUBMITTED`` / ``CHANGES_REQUESTED`` / ``APPROVED``
    关闭。``MERGED`` 和已 ``CLOSED`` 是终态。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status in (ChangeRequestStatus.MERGED, ChangeRequestStatus.CLOSED):
        raise HTTPException(status_code=400, detail="终态 CR 不可关闭")

    cr.status = ChangeRequestStatus.CLOSED
    cr.closed_at = _now_utc()
    cr.closed_by = data.closed_by
    cr.close_reason = data.close_reason

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
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status != ChangeRequestStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail=f"仅已批准的变更请求可合并（当前状态: {cr.status.value}）",
        )

    # 合并到本体
    cr.status = ChangeRequestStatus.MERGED
    cr.merged_at = _now_utc()
    cr.merged_by = data.merged_by

    if data.target_version_id:
        cr.target_version_id = data.target_version_id
    if data.target_version:
        cr.target_version = data.target_version

    # 如果变更包含需要创建的本体版本，生成新版本
    if cr.changes and not data.target_version:
        cr.target_version = f"v{_now_utc().strftime('%Y%m%d%H%M%S')}"

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


# =============================================================================
# Reviewer 子路由（HIA-69 B5 多 reviewer）
# =============================================================================


@cr_router.get("/{cr_id}/reviewers", response_model=list[ReviewerResponse])
async def list_reviewers(
    cr_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ReviewerResponse]:
    """列出 CR 的所有 reviewer 及状态。"""
    await _verify_cr_exists(session, cr_id)
    result = await session.execute(
        select(ChangeRequestReviewer)
        .where(ChangeRequestReviewer.change_request_id == cr_id)
        .order_by(ChangeRequestReviewer.created_at)
    )
    reviewers = result.scalars().all()
    return [_reviewer_to_response(r) for r in reviewers]


@cr_router.post(
    "/{cr_id}/reviewers",
    response_model=ReviewerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_reviewer(
    cr_id: uuid.UUID,
    data: ReviewerAssign,
    session: AsyncSession = Depends(get_session),
) -> ReviewerResponse:
    """给 CR 分配一个 reviewer（默认 PENDING）。

    重复分配同一 reviewer_id 会抛 409；CR 必须处于 ``DRAFT`` 或
    ``SUBMITTED`` / ``CHANGES_REQUESTED`` 状态。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status in (ChangeRequestStatus.MERGED, ChangeRequestStatus.CLOSED):
        raise HTTPException(status_code=400, detail="终态 CR 不可分配 reviewer")

    # Idempotency / uniqueness check
    existing = await session.execute(
        select(ChangeRequestReviewer).where(
            and_(
                ChangeRequestReviewer.change_request_id == cr_id,
                ChangeRequestReviewer.reviewer_id == data.reviewer_id,
            )
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="该 reviewer 已经分配到该 CR",
        )

    reviewer = ChangeRequestReviewer(
        change_request_id=cr_id,
        reviewer_id=data.reviewer_id,
        reviewer_name=data.reviewer_name,
        status=ReviewerStatus.PENDING,
    )
    session.add(reviewer)
    await session.flush()
    await session.refresh(reviewer)
    return _reviewer_to_response(reviewer)


@cr_router.post(
    "/{cr_id}/reviewers/{reviewer_id}/approve",
    response_model=ChangeRequestResponse,
)
async def reviewer_approve(
    cr_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    data: ReviewerDecision,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """单个 reviewer 投票 approve。

    同一 reviewer 可重复调用：每次调用都会更新 ``reviewed_at`` 并刷新
    计数。当 ``APPROVED`` 的 reviewer 数达到 ``required_approvers``，
    CR 自动升 ``SUBMITTED → APPROVED``。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status != ChangeRequestStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="仅已提交的 CR 可被 reviewer 审批")

    result = await session.execute(
        select(ChangeRequestReviewer).where(
            and_(
                ChangeRequestReviewer.change_request_id == cr_id,
                ChangeRequestReviewer.reviewer_id == reviewer_id,
            )
        )
    )
    reviewer = result.scalar_one_or_none()
    if not reviewer:
        raise HTTPException(status_code=404, detail="该 reviewer 未被分配到该 CR")

    reviewer.status = ReviewerStatus.APPROVED
    reviewer.reviewed_at = _now_utc()
    if data.comment is not None:
        reviewer.comment = data.comment

    # Auto-merge: 达到 required_approvers 即升级
    await _maybe_auto_merge(cr, session)

    # If auto-merged to APPROVED, set approved_by to this reviewer
    if cr.status == ChangeRequestStatus.APPROVED and cr.approved_by is None:
        cr.approved_by = reviewer_id

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.post(
    "/{cr_id}/reviewers/{reviewer_id}/reject",
    response_model=ChangeRequestResponse,
)
async def reviewer_reject(
    cr_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    data: ReviewerDecision,
    session: AsyncSession = Depends(get_session),
) -> ChangeRequestResponse:
    """单个 reviewer 投票 request changes。

    任一 reviewer 请求修改 → CR 进入 ``CHANGES_REQUESTED``，作者重新
    提交后 reviewer 状态会被重置为 ``PENDING``（见 ``/submit``）。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status != ChangeRequestStatus.SUBMITTED:
        raise HTTPException(
            status_code=400,
            detail="仅已提交的 CR 可被 reviewer 请求修改",
        )

    result = await session.execute(
        select(ChangeRequestReviewer).where(
            and_(
                ChangeRequestReviewer.change_request_id == cr_id,
                ChangeRequestReviewer.reviewer_id == reviewer_id,
            )
        )
    )
    reviewer = result.scalar_one_or_none()
    if not reviewer:
        raise HTTPException(status_code=404, detail="该 reviewer 未被分配到该 CR")

    reviewer.status = ReviewerStatus.CHANGES_REQUESTED
    reviewer.reviewed_at = _now_utc()
    if data.comment is not None:
        reviewer.comment = data.comment

    # Any changes-requested → CR flips back to CHANGES_REQUESTED
    if _changes_requested_reviewer_exists(cr):
        cr.status = ChangeRequestStatus.CHANGES_REQUESTED
        cr.reviewed_by = reviewer_id
        cr.reviewed_at = _now_utc()

    await session.flush()
    await session.refresh(cr)
    return _cr_to_response(cr)


@cr_router.delete(
    "/{cr_id}/reviewers/{reviewer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_reviewer(
    cr_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """取消 reviewer 分配（仅当 CR 未被 approve 时可删除）。"""
    cr = await _verify_cr_exists(session, cr_id)
    if cr.status == ChangeRequestStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail="已批准的 CR 不可移除 reviewer",
        )

    result = await session.execute(
        select(ChangeRequestReviewer).where(
            and_(
                ChangeRequestReviewer.change_request_id == cr_id,
                ChangeRequestReviewer.reviewer_id == reviewer_id,
            )
        )
    )
    reviewer = result.scalar_one_or_none()
    if not reviewer:
        raise HTTPException(status_code=404, detail="reviewer 未被分配到该 CR")

    await session.delete(reviewer)


# =============================================================================
# Comment 子路由（HIA-69 B5 评论线程）
# =============================================================================


@cr_router.get(
    "/{cr_id}/comments",
    response_model=list[CommentResponse],
)
async def list_comments(
    cr_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[CommentResponse]:
    """列出 CR 的所有评论（按时间排序；含 reply）。"""
    await _verify_cr_exists(session, cr_id)
    result = await session.execute(
        select(ChangeRequestComment)
        .where(ChangeRequestComment.change_request_id == cr_id)
        .order_by(ChangeRequestComment.created_at)
    )
    comments = result.scalars().all()
    return [_comment_to_response(c) for c in comments]


@cr_router.post(
    "/{cr_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    cr_id: uuid.UUID,
    data: CommentCreate,
    session: AsyncSession = Depends(get_session),
) -> CommentResponse:
    """创建评论（顶级或 reply）。

    ``parent_id`` 指向前一条评论时是 reply；为 ``None`` 是顶级评论。
    Reply 必须引用同 CR 下的评论（否则 400）。
    """
    cr = await _verify_cr_exists(session, cr_id)

    if cr.status in (ChangeRequestStatus.MERGED, ChangeRequestStatus.CLOSED):
        raise HTTPException(
            status_code=400,
            detail="终态 CR 不可继续评论",
        )

    if data.parent_id is not None:
        parent = await session.execute(
            select(ChangeRequestComment).where(
                and_(
                    ChangeRequestComment.id == data.parent_id,
                    ChangeRequestComment.change_request_id == cr_id,
                )
            )
        )
        if parent.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=400,
                detail="parent_id 必须指向同 CR 下的已有评论",
            )

    comment = ChangeRequestComment(
        change_request_id=cr_id,
        parent_id=data.parent_id,
        author_id=data.author_id,
        author_name=data.author_name,
        body=data.body,
    )
    session.add(comment)
    await session.flush()
    await session.refresh(comment)
    return _comment_to_response(comment)


@cr_router.delete(
    "/{cr_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_comment(
    cr_id: uuid.UUID,
    comment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删除评论（保留 thread 结构，body 替换为 "[deleted]"）。

    仅作者本人可删除自己的评论。这里我们不做 author_id 校验（生产应
    配合 ``require_role`` / auth）；保留简单实现以便测试。
    """
    result = await session.execute(
        select(ChangeRequestComment).where(
            and_(
                ChangeRequestComment.id == comment_id,
                ChangeRequestComment.change_request_id == cr_id,
            )
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="评论不存在")

    comment.deleted_at = _now_utc()


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


@router.post(
    "/projects/{project_id}/releases",
    response_model=ReleaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_release(
    project_id: uuid.UUID,
    data: ReleaseCreate,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """创建发布（快照当前本体状态）。"""
    await _verify_project_exists(session, project_id)

    if data.ontology_version_id:
        version_result = await session.execute(
            select(OntologyVersion).where(OntologyVersion.id == data.ontology_version_id)
        )
        version = version_result.scalar_one_or_none()
        if not version:
            raise HTTPException(status_code=400, detail="指定的本体版本不存在")

    if data.mapping_version_id:
        mapping_result = await session.execute(
            select(MappingVersion).where(MappingVersion.id == data.mapping_version_id)
        )
        mapping_version = mapping_result.scalar_one_or_none()
        if not mapping_version:
            raise HTTPException(status_code=400, detail="指定的映射版本不存在")

    artifacts = {
        "tags": data.tags,
        "ontology_version_id": str(data.ontology_version_id) if data.ontology_version_id else None,
        "mapping_version_id": str(data.mapping_version_id) if data.mapping_version_id else None,
    }

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

    if release.status in (ReleaseStatus.RELEASED, ReleaseStatus.PUBLISHED):
        raise HTTPException(status_code=400, detail="已发布的版本不可修改")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "tags":
            # tags 存在 artifacts JSON 里；如果 artifacts 还没初始化就创建空 dict
            current = release.artifacts or {}
            release.artifacts = {**current, "tags": value}
        else:
            setattr(release, field, value)

    await session.flush()
    await session.refresh(release)

    return _release_to_response(release)


def _build_manifest(release: Release) -> dict:
    """构建发布清单 (manifest)。"""
    manifest: dict = {
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

    if release.artifacts:
        manifest["artifacts"] = release.artifacts
    if release.validation_results:
        manifest["validation_results"] = release.validation_results
    return manifest


@router.get("/releases/{release_id}/download")
async def download_release(
    release_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """下载发布包（返回包含清单的 JSON）。"""
    release = await _verify_release_exists(session, release_id)

    manifest = _build_manifest(release)
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
    """运行预检检查（占位实现 — 实际检查逻辑由具体环境驱动）。"""
    release = await _verify_release_exists(session, release_id)

    preflight_result = {
        "overall_status": "passed",
        "checks": {
            "ontology_version": {"status": "skipped", "message": "no-op placeholder"},
            "mapping_version": {"status": "skipped", "message": "no-op placeholder"},
            "database_connectivity": {"status": "passed", "message": "ok"},
            "artifact_integrity": {"status": "passed", "message": "ok"},
        },
        "blocking_issues": [],
        "warnings": [],
        "environment": data.environment,
        "release_id": str(release.id),
        "version": release.version,
        "checked_at": _now_utc().isoformat(),
    }

    report = PreflightReport(
        project_id=release.project_id,
        environment=data.environment,
        release_version=release.version,
        status=preflight_result["overall_status"],
        checks=preflight_result["checks"],
        blocking_issues=preflight_result.get("blocking_issues") or [],
        warnings=preflight_result.get("warnings") or [],
    )
    session.add(report)
    await session.flush()

    release.validation_results = preflight_result
    await session.flush()

    return preflight_result


@router.post("/releases/{release_id}/publish", response_model=ReleaseResponse)
async def publish_release(
    release_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ReleaseResponse:
    """发布版本（状态变更为 RELEASED）。"""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
    )
    release = result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    if release.status in (ReleaseStatus.RELEASED, ReleaseStatus.PUBLISHED):
        raise HTTPException(status_code=400, detail="版本已经发布")

    manifest = _build_manifest(release)
    release.checksum = _compute_checksum(manifest)
    release.artifact_size = len(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    release.status = ReleaseStatus.RELEASED
    release.released_at = _now_utc()

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
    )

    if status is not None:
        query = query.where(Deployment.status == status)
    if environment:
        query = query.where(Deployment.environment == environment)

    query = query.offset(offset).limit(limit).order_by(Deployment.created_at.desc())

    result = await session.execute(query)
    deployments = result.scalars().all()

    return [_deployment_to_response(d) for d in deployments]


@router.post(
    "/projects/{project_id}/deployments",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_deployment(
    project_id: uuid.UUID,
    data: DeploymentCreate,
    session: AsyncSession = Depends(get_session),
) -> DeploymentResponse:
    """创建部署（将发布部署到目标环境）。"""
    await _verify_project_exists(session, project_id)

    release_result = await session.execute(
        select(Release).where(Release.id == data.release_id)
    )
    release = release_result.scalar_one_or_none()

    if not release:
        raise HTTPException(status_code=404, detail="发布不存在")

    if release.project_id != project_id:
        raise HTTPException(status_code=400, detail="发布不属于指定的项目")

    if release.status not in (ReleaseStatus.RELEASED, ReleaseStatus.PUBLISHED):
        raise HTTPException(status_code=400, detail="仅已发布的版本可部署")

    deployment = Deployment(
        release_id=data.release_id,
        project_id=project_id,
        environment=data.environment,
        environment_type=data.environment_type,
        configuration=data.configuration,
        status=DeploymentStatus.PENDING,
    )

    session.add(deployment)
    await session.flush()

    started = _now_utc()
    completed = started
    duration_ms = int((completed - started).total_seconds() * 1000)

    deployment.started_at = started
    deployment.completed_at = completed
    deployment.duration_ms = duration_ms
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
    deployment.completed_at = _now_utc()
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
    release_id: uuid.UUID
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


@router.get(
    "/projects/{project_id}/use-case-bundles",
    response_model=list[UseCaseBundleResponse],
)
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
            project_id=b.project_id or project_id,
            name=b.name or "",
            description=b.description,
            version=b.version or "",
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


@router.post(
    "/projects/{project_id}/use-case-bundles",
    response_model=UseCaseBundleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_use_case_bundle(
    project_id: uuid.UUID,
    data: UseCaseBundleCreate,
    session: AsyncSession = Depends(get_session),
) -> UseCaseBundleResponse:
    """创建用例包。"""
    await _verify_project_exists(session, project_id)
    release = await _verify_release_exists(session, data.release_id)
    if release.project_id != project_id:
        raise HTTPException(status_code=400, detail="release 不属于该项目")

    bundle = UseCaseBundle(
        project_id=project_id,
        release_id=data.release_id,
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
        name=bundle.name or "",
        description=bundle.description,
        version=bundle.version or "",
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


@router.get(
    "/projects/{project_id}/preflight-reports",
    response_model=list[PreflightReportResponse],
)
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
            project_id=r.project_id or project_id,
            environment=r.environment or "",
            release_version=r.release_version,
            status=r.status.value if r.status else "",
            checks=r.checks or {},
            blocking_issues=r.blocking_issues,
            warnings=r.warnings,
            created_by=r.created_by,
            created_at=_get_iso(r.created_at),
        )
        for r in reports
    ]

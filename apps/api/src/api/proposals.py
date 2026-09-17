"""候选提案 API 路由"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.candidate import (
    Proposal,
    ModelRun,
    ReviewDecision,
    ProposalStatus,
    ProposalType,
    ConfidenceLevel,
)

router = APIRouter(prefix="/proposals", tags=["候选提案"])


# ============ Pydantic 模型 ============

class ProposalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    proposal_type: ProposalType
    suggested_iri: Optional[str] = None
    content: Optional[dict] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    source_context: Optional[dict] = None
    rationale: Optional[str] = None
    model_name: Optional[str] = None


class ProposalUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    suggested_iri: Optional[str] = None
    content: Optional[dict] = None
    rationale: Optional[str] = None


class ProposalResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: Optional[str]
    proposal_type: ProposalType
    status: ProposalStatus
    suggested_iri: Optional[str]
    confidence: ConfidenceLevel
    confidence_score: Optional[float]
    source: Optional[str]
    model_name: Optional[str]
    reviewed_by: Optional[uuid.UUID]
    reviewed_at: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


class ReviewDecisionCreate(BaseModel):
    decision: str = Field(..., description="接受/拒绝/合并")
    notes: Optional[str] = None
    target_class_id: Optional[uuid.UUID] = None
    target_property_id: Optional[uuid.UUID] = None


class ReviewDecisionResponse(BaseModel):
    id: uuid.UUID
    decision: str
    notes: Optional[str]
    decided_by: uuid.UUID
    decided_by_name: Optional[str]
    target_class_id: Optional[uuid.UUID]
    target_property_id: Optional[uuid.UUID]
    proposal_version: int
    created_at: str

    model_config = {"from_attributes": True}


# ============ 提案路由 ============

@router.get("", response_model=list[ProposalResponse])
async def list_proposals(
    session: AsyncSession = Depends(get_session),
    project_id: Optional[uuid.UUID] = Query(None),
    status: Optional[ProposalStatus] = Query(None),
    proposal_type: Optional[ProposalType] = Query(None),
    confidence: Optional[ConfidenceLevel] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ProposalResponse]:
    query = select(Proposal)
    
    if project_id:
        query = query.where(Proposal.project_id == project_id)
    if status:
        query = query.where(Proposal.status == status)
    if proposal_type:
        query = query.where(Proposal.proposal_type == proposal_type)
    if confidence:
        query = query.where(Proposal.confidence == confidence)
    if search:
        query = query.where(Proposal.title.ilike(f"%{search}%"))
    
    query = query.offset(offset).limit(limit).order_by(Proposal.created_at.desc())
    
    result = await session.execute(query)
    proposals = result.scalars().all()
    
    return [
        ProposalResponse(
            id=p.id,
            title=p.title,
            description=p.description,
            proposal_type=p.proposal_type,
            status=p.status,
            suggested_iri=p.suggested_iri,
            confidence=p.confidence,
            confidence_score=p.confidence_score,
            source=p.source,
            model_name=p.model_name,
            reviewed_by=p.reviewed_by,
            reviewed_at=p.reviewed_at.isoformat() if p.reviewed_at else None,
            created_at=p.created_at.isoformat() if p.created_at else "",
        )
        for p in proposals
    ]


@router.post("", response_model=ProposalResponse, status_code=status.HTTP_201_CREATED)
async def create_proposal(
    data: ProposalCreate,
    session: AsyncSession = Depends(get_session),
    project_id: Optional[uuid.UUID] = Query(None),
) -> ProposalResponse:
    proposal = Proposal(
        project_id=project_id,
        title=data.title,
        description=data.description,
        proposal_type=data.proposal_type,
        suggested_iri=data.suggested_iri,
        content=data.content,
        source=data.source,
        source_id=data.source_id,
        source_context=data.source_context,
        rationale=data.rationale,
        model_name=data.model_name,
        status=ProposalStatus.PENDING,
        confidence=ConfidenceLevel.UNCALIBRATED,
    )
    
    session.add(proposal)
    await session.flush()
    await session.refresh(proposal)
    
    return ProposalResponse(
        id=proposal.id,
        title=proposal.title,
        description=proposal.description,
        proposal_type=proposal.proposal_type,
        status=proposal.status,
        suggested_iri=proposal.suggested_iri,
        confidence=proposal.confidence,
        confidence_score=proposal.confidence_score,
        source=proposal.source,
        model_name=proposal.model_name,
        reviewed_by=proposal.reviewed_by,
        reviewed_at=proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
        created_at=proposal.created_at.isoformat() if proposal.created_at else "",
    )


@router.get("/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ProposalResponse:
    result = await session.execute(
        select(Proposal).where(Proposal.id == proposal_id)
    )
    proposal = result.scalar_one_or_none()
    
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    
    return ProposalResponse(
        id=proposal.id,
        title=proposal.title,
        description=proposal.description,
        proposal_type=proposal.proposal_type,
        status=proposal.status,
        suggested_iri=proposal.suggested_iri,
        confidence=proposal.confidence,
        confidence_score=proposal.confidence_score,
        source=proposal.source,
        model_name=proposal.model_name,
        reviewed_by=proposal.reviewed_by,
        reviewed_at=proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
        created_at=proposal.created_at.isoformat() if proposal.created_at else "",
    )


@router.patch("/{proposal_id}", response_model=ProposalResponse)
async def update_proposal(
    proposal_id: uuid.UUID,
    data: ProposalUpdate,
    session: AsyncSession = Depends(get_session),
) -> ProposalResponse:
    result = await session.execute(
        select(Proposal).where(Proposal.id == proposal_id)
    )
    proposal = result.scalar_one_or_none()
    
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    
    if proposal.status != ProposalStatus.PENDING:
        raise HTTPException(status_code=400, detail="只能修改待处理的提案")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(proposal, field, value)
    
    await session.flush()
    await session.refresh(proposal)
    
    return ProposalResponse(
        id=proposal.id,
        title=proposal.title,
        description=proposal.description,
        proposal_type=proposal.proposal_type,
        status=proposal.status,
        suggested_iri=proposal.suggested_iri,
        confidence=proposal.confidence,
        confidence_score=proposal.confidence_score,
        source=proposal.source,
        model_name=proposal.model_name,
        reviewed_by=proposal.reviewed_by,
        reviewed_at=proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
        created_at=proposal.created_at.isoformat() if proposal.created_at else "",
    )


@router.delete("/{proposal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(Proposal).where(Proposal.id == proposal_id)
    )
    proposal = result.scalar_one_or_none()
    
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    
    if proposal.status != ProposalStatus.PENDING:
        raise HTTPException(status_code=400, detail="只能删除待处理的提案")
    
    await session.delete(proposal)


# ============ 审核决策路由 ============

@router.get("/{proposal_id}/decisions", response_model=list[ReviewDecisionResponse])
async def list_decisions(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ReviewDecisionResponse]:
    result = await session.execute(
        select(ReviewDecision)
        .where(ReviewDecision.proposal_id == proposal_id)
        .order_by(ReviewDecision.created_at.desc())
    )
    decisions = result.scalars().all()
    
    return [
        ReviewDecisionResponse(
            id=d.id,
            decision=d.decision,
            notes=d.notes,
            decided_by=d.decided_by,
            decided_by_name=d.decided_by_name,
            target_class_id=d.target_class_id,
            target_property_id=d.target_property_id,
            proposal_version=d.proposal_version,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in decisions
    ]


@router.post("/{proposal_id}/decisions", response_model=ReviewDecisionResponse, status_code=status.HTTP_201_CREATED)
async def create_decision(
    proposal_id: uuid.UUID,
    data: ReviewDecisionCreate,
    session: AsyncSession = Depends(get_session),
) -> ReviewDecisionResponse:
    # 获取提案
    proposal_result = await session.execute(
        select(Proposal).where(Proposal.id == proposal_id)
    )
    proposal = proposal_result.scalar_one_or_none()
    
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    
    if proposal.status != ProposalStatus.PENDING:
        raise HTTPException(status_code=400, detail="只能审核待处理的提案")
    
    # 更新提案状态
    if data.decision == "accept":
        proposal.status = ProposalStatus.ACCEPTED
    elif data.decision == "reject":
        proposal.status = ProposalStatus.REJECTED
    elif data.decision == "merge":
        proposal.status = ProposalStatus.MERGED
    
    proposal.reviewed_at = proposal.updated_at
    
    # 创建决策记录
    decision = ReviewDecision(
        proposal_id=proposal_id,
        decision=data.decision,
        notes=data.notes,
        decided_by=uuid.uuid4(),  # TODO: 从认证获取
        decided_by_name="System User",  # TODO: 从认证获取
        target_class_id=data.target_class_id,
        target_property_id=data.target_property_id,
        proposal_version=proposal.version,
    )
    
    session.add(decision)
    await session.flush()
    await session.refresh(decision)
    
    return ReviewDecisionResponse(
        id=decision.id,
        decision=decision.decision,
        notes=decision.notes,
        decided_by=decision.decided_by,
        decided_by_name=decision.decided_by_name,
        target_class_id=decision.target_class_id,
        target_property_id=decision.target_property_id,
        proposal_version=decision.proposal_version,
        created_at=decision.created_at.isoformat() if decision.created_at else "",
    )


# ============ 批量操作 ============

@router.post("/batch-review", status_code=status.HTTP_200_OK)
async def batch_review_proposals(
    proposal_ids: list[uuid.UUID],
    decision: str = Query(..., description="accept/reject"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """批量审核提案"""
    if decision not in ["accept", "reject"]:
        raise HTTPException(status_code=400, detail="无效的决策")
    
    result = await session.execute(
        select(Proposal).where(Proposal.id.in_(proposal_ids))
    )
    proposals = result.scalars().all()
    
    accepted = 0
    rejected = 0
    skipped = 0
    
    for proposal in proposals:
        if proposal.status != ProposalStatus.PENDING:
            skipped += 1
            continue
        
        if decision == "accept":
            proposal.status = ProposalStatus.ACCEPTED
            accepted += 1
        else:
            proposal.status = ProposalStatus.REJECTED
            rejected += 1
    
    await session.flush()
    
    return {
        "total": len(proposal_ids),
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
    }

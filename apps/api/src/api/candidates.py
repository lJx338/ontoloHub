"""候选提案生成 API 路由

提供从数据源/证据自动生成提案的端点。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.evidence import Source
from src.db.candidate import Proposal
from src.services.candidates import (
    generate_candidates_from_profiling,
    generate_candidates_from_evidence,
)


router = APIRouter(prefix="/candidates", tags=["候选生成"])


# =====================================================================
# Pydantic
# =====================================================================


class GenerateFromProfilingRequest(BaseModel):
    """从剖析结果生成提案"""

    ontology_id: Optional[uuid.UUID] = None
    namespace_base: str = Field(
        default="http://example.org/onto#",
        max_length=500,
    )
    batch_size: int = Field(default=100, ge=1, le=500)


class GenerateFromEvidenceRequest(BaseModel):
    """从证据生成提案"""

    ontology_id: Optional[uuid.UUID] = None
    namespace_base: str = Field(
        default="http://example.org/onto#",
        max_length=500,
    )


class CandidateProfileItem(BaseModel):
    """字段剖析档案"""

    field_name: str
    inferred_type: str
    confidence: float
    confidence_level: str


class CandidateGenerationResponse(BaseModel):
    """生成结果"""

    proposals_created: int
    proposals_skipped: int
    field_profiles: list[CandidateProfileItem]


# =====================================================================
# 路由
# =====================================================================


@router.post("/from-profiling/{source_id}", response_model=CandidateGenerationResponse)
async def generate_from_profiling(
    source_id: uuid.UUID,
    data: GenerateFromProfilingRequest,
    session: AsyncSession = Depends(get_session),
) -> CandidateGenerationResponse:
    """从源字段剖析结果生成候选提案

    行为：
    - 读取源的 schema_info 字段定义
    - 对每个字段推断语义类型（identifier / label / quantity / enumeration / datetime 等）
    - 为推断为非标识符的字段创建提案（ProposalType=property）
    - 返回各字段的剖析档案和生成统计
    """
    # 确认源存在
    src_result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    if not src_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="数据源不存在")

    try:
        result = await generate_candidates_from_profiling(
            session=session,
            source_id=source_id,
            ontology_id=data.ontology_id,
            namespace_base=data.namespace_base,
            batch_size=data.batch_size,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return CandidateGenerationResponse(
        proposals_created=result.proposals_created,
        proposals_skipped=result.proposals_skipped,
        field_profiles=[
            CandidateProfileItem(**fp) for fp in result.field_profiles
        ],
    )


@router.post("/from-evidence/project/{project_id}", response_model=CandidateGenerationResponse)
async def generate_from_evidence(
    project_id: uuid.UUID,
    data: GenerateFromEvidenceRequest,
    session: AsyncSession = Depends(get_session),
) -> CandidateGenerationResponse:
    """从项目未对齐证据批量生成提案

    读取所有 is_confirmed=False 且未对齐 ontology_class_iri 的证据，
    按字段名分组后，为每组生成一条提案。
    """
    try:
        result = await generate_candidates_from_evidence(
            session=session,
            project_id=project_id,
            ontology_id=data.ontology_id,
            namespace_base=data.namespace_base,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return CandidateGenerationResponse(
        proposals_created=result.proposals_created,
        proposals_skipped=result.proposals_skipped,
        field_profiles=[
            CandidateProfileItem(**fp) for fp in result.field_profiles
        ],
    )


@router.get("/stats/project/{project_id}")
async def candidate_stats(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """获取项目候选提案统计"""
    from sqlalchemy import func, select
    from src.db.candidate import ProposalStatus, ProposalType, ConfidenceLevel

    stats_result = await session.execute(
        select(
            Proposal.status,
            Proposal.proposal_type,
            func.count(Proposal.id).label("count"),
        )
        .where(Proposal.project_id == project_id)
        .group_by(Proposal.status, Proposal.proposal_type)
    )
    rows = stats_result.all()

    by_status: dict[str, dict[str, int]] = {}
    total = 0
    for row in rows:
        s = row.status.value
        t = row.proposal_type.value
        if s not in by_status:
            by_status[s] = {}
        by_status[s][t] = row.count
        total += row.count

    # 置信度分布
    conf_result = await session.execute(
        select(
            Proposal.confidence,
            func.count(Proposal.id).label("count"),
        )
        .where(Proposal.project_id == project_id)
        .group_by(Proposal.confidence)
    )
    conf_rows = conf_result.all()
    by_confidence = {r.confidence.value: r.count for r in conf_rows}

    return {
        "project_id": str(project_id),
        "total": total,
        "by_status": by_status,
        "by_confidence": by_confidence,
    }

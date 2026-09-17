"""Candidate / proposal / model run models (AI-suggested mappings & decisions)."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Float, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin

if TYPE_CHECKING:
    from .project import Project
    from .ontology import OntologyClass, Property


class ProposalStatus(str, Enum):
    """Lifecycle of a single candidate proposal."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MERGED = "merged"
    SUPERSEDED = "superseded"


class ProposalType(str, Enum):
    """Kind of entity the proposal introduces."""

    CLASS = "class"
    PROPERTY = "property"
    RELATION = "relation"
    CONSTRAINT = "constraint"
    MAPPING = "mapping"


class ConfidenceLevel(str, Enum):
    """Calibrated confidence bucket for a proposal."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCALIBRATED = "uncalibrated"


class Proposal(Base, UUIDMixin, TimestampMixin):
    """A candidate mapping / ontology element proposed by rule-based or LLM analysis."""

    __tablename__ = "proposals"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    proposal_type: Mapped[ProposalType] = mapped_column(
        SQLEnum(ProposalType), nullable=False
    )
    status: Mapped[ProposalStatus] = mapped_column(
        SQLEnum(ProposalStatus),
        default=ProposalStatus.PENDING,
        nullable=False,
    )

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    ontology_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    content: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    suggested_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SQLEnum(ConfidenceLevel),
        default=ConfidenceLevel.UNCALIBRATED,
        nullable=False,
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    similar_concepts: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    model_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    impact_scope: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    merged_into: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_proposal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_proposals_project", "project_id"),
        Index("ix_proposals_status", "status"),
        Index("ix_proposals_type", "proposal_type"),
    )

    model_runs: Mapped[list["ModelRun"]] = relationship(
        "ModelRun", back_populates="proposal", cascade="all, delete-orphan"
    )
    review_decisions: Mapped[list["ReviewDecision"]] = relationship(
        "ReviewDecision", back_populates="proposal", cascade="all, delete-orphan"
    )


class ModelRun(Base, UUIDMixin, TimestampMixin):
    """One execution of the candidate generation pipeline that produced a proposal."""

    __tablename__ = "model_runs"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    parsed_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="completed")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    proposal: Mapped["Proposal"] = relationship("Proposal", back_populates="model_runs")


class ReviewDecision(Base, UUIDMixin, TimestampMixin):
    """A human reviewer decision against a proposal at a given version."""

    __tablename__ = "review_decisions"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    decided_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    target_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_property_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)

    proposal: Mapped["Proposal"] = relationship(
        "Proposal", back_populates="review_decisions"
    )
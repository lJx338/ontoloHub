"""Project domain models.

M0 baseline: minimal but coherent schema covering Project / UseCase / Requirement / Decision.
Full feature set (rich status workflows, business owner, etc.) will be expanded in HIA-51+.
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Enum as SQLEnum, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin, ProjectMixin

if TYPE_CHECKING:
    from .ontology import Ontology, OntologyVersion
    from .evidence import Source, Evidence
    from .mapping import MappingVersion
    from .release import ChangeRequest, Release


class ProjectStatus(str, Enum):
    """Project lifecycle status."""

    DISCOVERY = "discovery"
    MODELING = "modeling"
    VALIDATION = "validation"
    DELIVERY = "delivery"
    SUPPORT = "support"
    ARCHIVED = "archived"


class Project(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """Top-level business project that scopes all ontology / evidence / mapping work."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[ProjectStatus] = mapped_column(
        SQLEnum(ProjectStatus),
        default=ProjectStatus.DISCOVERY,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(default=0)

    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    target_environment: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    business_owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    business_owner_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_customer", "customer_id"),
    )

    use_cases: Mapped[list["UseCase"]] = relationship(
        "UseCase", back_populates="project", cascade="all, delete-orphan"
    )
    sources: Mapped[list["Source"]] = relationship(
        "Source", back_populates="project", cascade="all, delete-orphan"
    )
    ontologies: Mapped[list["Ontology"]] = relationship(
        "Ontology", back_populates="project", cascade="all, delete-orphan"
    )
    mapping_versions: Mapped[list["MappingVersion"]] = relationship(
        "MappingVersion", back_populates="project", cascade="all, delete-orphan"
    )
    change_requests: Mapped[list["ChangeRequest"]] = relationship(
        "ChangeRequest", back_populates="project", cascade="all, delete-orphan"
    )
    releases: Mapped[list["Release"]] = relationship(
        "Release", back_populates="project", cascade="all, delete-orphan"
    )


class UseCase(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """Use case attached to a Project."""

    __tablename__ = "use_cases"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    business_problem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acceptance_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="draft")
    importance: Mapped[str] = mapped_column(String(50), default="medium")

    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    exclusions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    open_issues: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="use_cases")
    requirements: Mapped[list["Requirement"]] = relationship(
        "Requirement", back_populates="use_case", cascade="all, delete-orphan"
    )


class Requirement(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """Requirement answering a business question; may belong to a UseCase."""

    __tablename__ = "requirements"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    use_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("use_cases.id", ondelete="SET NULL"),
        nullable=True,
    )

    business_question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    importance: Mapped[str] = mapped_column(String(50), default="medium")
    acceptance_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open")

    use_case: Mapped[Optional["UseCase"]] = relationship(
        "UseCase", back_populates="requirements"
    )


class Decision(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """Architecture / design decision record (ADR)."""

    __tablename__ = "decisions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    options_considered: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open")

    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    decided_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
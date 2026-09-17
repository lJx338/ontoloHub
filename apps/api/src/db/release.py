"""Release models ? change requests, releases, deployments, preflight reports."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project
    from .mapping import MappingVersion


class ChangeRequestStatus(str, Enum):
    """Lifecycle of a change request (CR)."""

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    SUPERSEDED = "superseded"


class ChangeRequestType(str, Enum):
    """Kind of change being requested."""

    ONTOLOGY = "ontology"
    MAPPING = "mapping"
    ACTION = "action"
    POLICY = "policy"


class ReleaseStatus(str, Enum):
    """Lifecycle of a release line."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


class DeploymentStatus(str, Enum):
    """Lifecycle of a deployment to a target environment."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class PreflightStatus(str, Enum):
    """Outcome of a preflight check."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class ChangeRequest(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A formal change request covering ontology / mapping / action changes."""

    __tablename__ = "change_requests"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    cr_type: Mapped[ChangeRequestType] = mapped_column(
        SQLEnum(ChangeRequestType), nullable=False
    )
    status: Mapped[ChangeRequestStatus] = mapped_column(
        SQLEnum(ChangeRequestStatus),
        default=ChangeRequestStatus.DRAFT,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Targets (one of, depending on cr_type)
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Diff snapshot for review
    diff: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Authoring
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    author_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Review
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reviewer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_change_requests_project_status", "project_id", "status"),
        Index("ix_change_requests_type", "cr_type"),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="change_requests")


class Release(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A release line bundling several change requests for a target environment."""

    __tablename__ = "releases"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[ReleaseStatus] = mapped_column(
        SQLEnum(ReleaseStatus),
        default=ReleaseStatus.PLANNED,
        nullable=False,
    )

    target_environment: Mapped[str] = mapped_column(String(50), nullable=False)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_releases_project_version", "project_id", "version", unique=True),
        Index("ix_releases_status", "status"),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="releases")
    bundles: Mapped[list["UseCaseBundle"]] = relationship(
        "UseCaseBundle", back_populates="release", cascade="all, delete-orphan"
    )


class UseCaseBundle(Base, UUIDMixin, TimestampMixin):
    """A bundle of use cases packaged together for a release."""

    __tablename__ = "use_case_bundles"

    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    use_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("use_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    priority: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    release: Mapped["Release"] = relationship("Release", back_populates="bundles")


class Deployment(Base, UUIDMixin, TimestampMixin):
    """A single deployment of a release to a target environment."""

    __tablename__ = "deployments"

    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    target_environment: Mapped[str] = mapped_column(String(50), nullable=False)

    status: Mapped[DeploymentStatus] = mapped_column(
        SQLEnum(DeploymentStatus),
        default=DeploymentStatus.PENDING,
        nullable=False,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    deployed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rollback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_deployments_release", "release_id"),
        Index("ix_deployments_status", "status"),
    )


class PreflightReport(Base, UUIDMixin, TimestampMixin):
    """Result of a preflight check before publishing a release."""

    __tablename__ = "preflight_reports"

    release_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    target_environment: Mapped[str] = mapped_column(String(50), nullable=False)

    status: Mapped[PreflightStatus] = mapped_column(
        SQLEnum(PreflightStatus),
        default=PreflightStatus.PASSED,
        nullable=False,
    )

    checks: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    blockers: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_preflight_release", "release_id"),
    )
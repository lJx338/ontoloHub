"""Release models — change requests, releases, deployments, preflight reports.

HIA-69 / B5 adds CR workflow enhancements:

* ``ChangeRequestStatus`` enum extended with ``SUBMITTED`` / ``CHANGES_REQUESTED`` / ``CLOSED``
* ``ChangeRequest`` gains the full state machine (``submitted_by`` / ``submitted_at``,
  ``approved_by`` / ``approved_at``, ``merged_by``, ``created_by``, ``changes``,
  ``changes_summary``, ``impact_scope``, ``required_approvers`` …)
* ``ChangeRequestReviewer`` — M2M table for multiple reviewers, each with
  individual status (pending / approved / changes_requested)
* ``ChangeRequestComment`` — threaded comments with ``parent_id``
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING, List

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
    Enum as SQLEnum,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project
    from .mapping import MappingVersion


class ChangeRequestStatus(str, Enum):
    """Lifecycle of a change request (CR).

    State machine (HIA-69 B5):

        DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──merge──▶ MERGED
          │                   │                       │
          │                   ├──changes_requested──▶ CHANGES_REQUESTED ──┐
          │                   │                                            │
          │                   └──close──▶ CLOSED ◀────────close────────────┤
          │                                                                │
          └──close──▶ CLOSED ◀────────────────────────────────────────────┘

    * ``DRAFT`` — author has saved but not submitted.
    * ``SUBMITTED`` — open / awaiting review (HIA-69 "open").
    * ``CHANGES_REQUESTED`` — reviewer asks for revision (loop back to DRAFT on re-submit).
    * ``APPROVED`` — all required_approvers signed off, awaiting merge.
    * ``MERGED`` — merged into ontology / mapping (terminal).
    * ``CLOSED`` — manually closed / superseded / abandoned (terminal).

    ``REVIEW`` / ``REJECTED`` / ``SUPERSEDED`` are kept as legacy aliases
    for old rows already in the DB.  New code should use the six values
    above.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVIEW = "review"  # legacy alias for SUBMITTED
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"  # legacy alias for CHANGES_REQUESTED
    APPROVED = "approved"
    MERGED = "merged"
    CLOSED = "closed"
    SUPERSEDED = "superseded"  # legacy alias for CLOSED


class ReviewerStatus(str, Enum):
    """Per-reviewer status on a ChangeRequest."""

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class ChangeRequestType(str, Enum):
    """Kind of change being requested (legacy field, kept for back-compat)."""

    ONTOLOGY = "ontology"
    MAPPING = "mapping"
    ACTION = "action"
    POLICY = "policy"


class ReleaseStatus(str, Enum):
    """Lifecycle of a release line."""

    DRAFT = "draft"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    PUBLISHED = "published"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


class DeploymentStatus(str, Enum):
    """Lifecycle of a deployment to a target environment."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SUCCEEDED = "succeeded"  # legacy alias for SUCCESS
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

    # ---- Legacy field kept for back-compat with pre-HIA-69 rows ----
    cr_type: Mapped[Optional[str]] = mapped_column(
        SQLEnum(ChangeRequestType),
        nullable=True,
    )

    # ---- Lifecycle ----
    status: Mapped[ChangeRequestStatus] = mapped_column(
        SQLEnum(ChangeRequestStatus),
        default=ChangeRequestStatus.DRAFT,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ---- Versioning targets (HIA-69) ----
    baseline_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    baseline_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Legacy ontology/mapping version pointers
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ---- Payload ----
    changes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    changes_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    impact_scope: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    diff: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ---- Approval configuration (HIA-69) ----
    required_approvers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ---- Authoring ----
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # legacy alias
    author_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ---- Submit ----
    submitted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Review ----
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(  # legacy
        UUID(as_uuid=True), nullable=True
    )
    reviewer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ---- Approval ----
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Merge ----
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ---- Close ----
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    close_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_change_requests_project_status", "project_id", "status"),
        Index("ix_change_requests_type", "cr_type"),
        Index("ix_change_requests_submitted_at", "submitted_at"),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="change_requests")
    reviewers: Mapped[List["ChangeRequestReviewer"]] = relationship(
        "ChangeRequestReviewer",
        back_populates="change_request",
        cascade="all, delete-orphan",
    )
    comments: Mapped[List["ChangeRequestComment"]] = relationship(
        "ChangeRequestComment",
        back_populates="change_request",
        cascade="all, delete-orphan",
        order_by="ChangeRequestComment.created_at",
    )


class ChangeRequestReviewer(Base, UUIDMixin, TimestampMixin):
    """A reviewer assigned to a ChangeRequest with their own approval status.

    HIA-69 / B5: enables multi-reviewer workflows where each reviewer can
    approve / request changes independently.  The CR auto-merges when the
    count of ``APPROVED`` reviewers reaches ``ChangeRequest.required_approvers``.
    """

    __tablename__ = "change_request_reviewers"

    change_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    reviewer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[ReviewerStatus] = mapped_column(
        SQLEnum(ReviewerStatus),
        default=ReviewerStatus.PENDING,
        nullable=False,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "change_request_id", "reviewer_id", name="uq_cr_reviewer_cr_reviewer"
        ),
        Index("ix_cr_reviewers_status", "status"),
    )

    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="reviewers"
    )


class ChangeRequestComment(Base, UUIDMixin, TimestampMixin):
    """A comment / reply thread on a ChangeRequest.

    HIA-69 / B5: ``parent_id`` enables GitHub-style threaded discussions.
    Top-level comments have ``parent_id is None``; replies reference their
    parent.  A simple adjacency-list model — good enough for the expected
    CR thread depth (≤ 3 levels).
    """

    __tablename__ = "change_request_comments"

    change_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("change_request_comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    author_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional soft-delete for "comment removed" without breaking thread structure
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_cr_comments_cr_created", "change_request_id", "created_at"),
    )

    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="comments"
    )
    replies: Mapped[List["ChangeRequestComment"]] = relationship(
        "ChangeRequestComment",
        cascade="all, delete-orphan",
        # remote_side is implicit via parent_id FK; we don't back_populate
        # parent to avoid cycles in serialization.
    )


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
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[ReleaseStatus] = mapped_column(
        SQLEnum(ReleaseStatus),
        default=ReleaseStatus.PLANNED,
        nullable=False,
    )

    # HIA-69: payload / manifest columns
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ontology_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mapping_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    artifacts: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    artifact_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    validation_results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Legacy fields
    target_environment: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
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
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Legacy link release_key
    use_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("use_cases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    use_case_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    validation_results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    dependencies: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    manifest: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    artifact_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
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
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # HIA-69: API expects ``environment`` not ``target_environment``.
    environment: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    environment_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_environment: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    status: Mapped[DeploymentStatus] = mapped_column(
        SQLEnum(DeploymentStatus),
        default=DeploymentStatus.PENDING,
        nullable=False,
    )

    configuration: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    deployed_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # legacy
    rollback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_deployments_release", "release_id"),
        Index("ix_deployments_status", "status"),
    )


class PreflightReport(Base, UUIDMixin, TimestampMixin):
    """Result of a preflight check before publishing a release."""

    __tablename__ = "preflight_reports"

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    release_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    environment: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    release_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_environment: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    status: Mapped[PreflightStatus] = mapped_column(
        SQLEnum(PreflightStatus),
        default=PreflightStatus.PASSED,
        nullable=False,
    )

    checks: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    blocking_issues: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    blockers: Mapped[int] = mapped_column(Integer, default=0)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_preflight_release", "release_id"),
        Index("ix_preflight_project", "project_id"),
    )
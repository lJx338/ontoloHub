"""Validation models ? fixtures, validation runs, saved queries, expected results."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, Float, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project
    from .ontology import OntologyVersion


class ValidationStatus(str, Enum):
    """Outcome of a validation run."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    ERROR = "error"


class FixtureKind(str, Enum):
    """Kind of validation fixture."""

    OBJECT = "object"
    DATASET = "dataset"
    SHACL_SHAPE = "shacl_shape"
    QUERY = "query"


class MappingFixture(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A reusable fixture (input data + expected results) for validation."""

    __tablename__ = "mapping_fixtures"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    kind: Mapped[FixtureKind] = mapped_column(
        SQLEnum(FixtureKind), default=FixtureKind.OBJECT, nullable=False
    )

    input_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expected_results: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_golden: Mapped[bool] = mapped_column(Boolean, default=False)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_mapping_fixtures_project_kind", "project_id", "kind"),
    )


class ValidationRun(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A single execution of a validation suite."""

    __tablename__ = "validation_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[ValidationStatus] = mapped_column(
        SQLEnum(ValidationStatus),
        default=ValidationStatus.PENDING,
        nullable=False,
    )

    target_type: Mapped[str] = mapped_column(String(50), nullable=False)  # mapping | ontology | dataset
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Inputs
    fixture_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    shape_paths: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Results
    total_checks: Mapped[int] = mapped_column(Integer, default=0)
    passed_checks: Mapped[int] = mapped_column(Integer, default=0)
    warning_checks: Mapped[int] = mapped_column(Integer, default=0)
    failed_checks: Mapped[int] = mapped_column(Integer, default=0)

    violations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    report_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    triggered_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_validation_runs_project_status", "project_id", "status"),
    )


class SavedQuery(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A reusable saved query (with parameter binding) over objects / links."""

    __tablename__ = "saved_queries"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    query: Mapped[str] = mapped_column(Text, nullable=False)
    parameters_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)

    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class ExpectedResult(Base, UUIDMixin, TimestampMixin):
    """An expected result row for a fixture / saved query used in regression checks."""

    __tablename__ = "expected_results"

    fixture_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mapping_fixtures.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    saved_query_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saved_queries.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    match_mode: Mapped[str] = mapped_column(String(50), default="exact")  # exact | subset | superset | approx

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
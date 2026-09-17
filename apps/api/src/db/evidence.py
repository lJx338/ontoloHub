"""Evidence & data source models (Evidence Inbox, profiling, etc.)."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, Float, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin

if TYPE_CHECKING:
    from .project import Project


class SourceType(str, Enum):
    """Data source kind (file format or DB)."""

    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"
    POSTGRESQL = "postgresql"
    TEXT = "text"
    MARKDOWN = "markdown"
    RDF = "rdf"  # TTL, OWL, NT


class SourceStatus(str, Enum):
    """Lifecycle of a registered data source."""

    UPLOADED = "uploaded"
    PARSED = "parsed"
    PROFILING = "profiling"
    READY = "ready"
    ERROR = "error"
    ARCHIVED = "archived"


class EvidenceType(str, Enum):
    """Kind of evidence row stored against a project."""

    SOURCE_FIELD = "source_field"
    SOURCE_RECORD = "source_record"
    DOCUMENT = "document"
    DOMAIN_EXPERT = "domain_expert"
    STANDARD = "standard"
    INFERRED = "inferred"


class ProfilingStatus(str, Enum):
    """Lifecycle of a profiling run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Source(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A registered data source (CSV / XLSX / PG / ...) attached to a project."""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_type: Mapped[SourceType] = mapped_column(SQLEnum(SourceType), nullable=False)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    connection_info: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[SourceStatus] = mapped_column(
        SQLEnum(SourceStatus),
        default=SourceStatus.UPLOADED,
        nullable=False,
    )

    access_scope: Mapped[str] = mapped_column(String(50), default="restricted")
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    schema_info: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    column_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_sources_project", "project_id"),
        Index("ix_sources_type", "source_type"),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="sources")
    snapshots: Mapped[list["SourceSnapshot"]] = relationship(
        "SourceSnapshot", back_populates="source", cascade="all, delete-orphan"
    )
    profiling_runs: Mapped[list["ProfilingRun"]] = relationship(
        "ProfilingRun", back_populates="source", cascade="all, delete-orphan"
    )
    evidences: Mapped[list["Evidence"]] = relationship(
        "Evidence", back_populates="source", cascade="all, delete-orphan"
    )


class SourceSnapshot(Base, UUIDMixin, TimestampMixin):
    """A snapshot of a source at a given moment (versioned row, immutable)."""

    __tablename__ = "source_snapshots"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(50), nullable=False)

    storage_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    storage_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    schema_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    source: Mapped["Source"] = relationship("Source", back_populates="snapshots")


class Evidence(Base, UUIDMixin, TimestampMixin):
    """A piece of evidence linking a source field/record to an ontology concept."""

    __tablename__ = "evidences"

    evidence_type: Mapped[EvidenceType] = mapped_column(
        SQLEnum(EvidenceType), nullable=False
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
    )

    location: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    field_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    record_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    source_identifier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    extraction_method: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extraction_params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    ontology_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ontology_class_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    property_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    property_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    strength: Mapped[str] = mapped_column(String(50), default="medium")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_evidences_project", "project_id"),
        Index("ix_evidences_source", "source_id"),
        Index("ix_evidences_ontology", "ontology_class_id", "property_id"),
    )

    source: Mapped[Optional["Source"]] = relationship("Source", back_populates="evidences")


class ProfilingRun(Base, UUIDMixin, TimestampMixin):
    """A single field-profiling execution against a source."""

    __tablename__ = "profiling_runs"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[ProfilingStatus] = mapped_column(
        SQLEnum(ProfilingStatus),
        default=ProfilingStatus.PENDING,
        nullable=False,
    )

    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    total_rows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sampled_rows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_columns: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    source: Mapped["Source"] = relationship("Source", back_populates="profiling_runs")
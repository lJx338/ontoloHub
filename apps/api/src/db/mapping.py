"""Mapping version / identity mapping / dataset snapshot models."""
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
    from .evidence import Source


class MappingStatus(str, Enum):
    """Lifecycle of a mapping version."""

    DRAFT = "draft"
    TESTED = "tested"
    VALIDATED = "validated"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class IdentityKeyType(str, Enum):
    """Kind of identity key an identity mapping represents."""

    PRIMARY = "primary"
    CANDIDATE = "candidate"
    NATURAL = "natural"
    SURROGATE = "surrogate"


class MappingVersion(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A versioned set of mappings from a source to an ontology version."""

    __tablename__ = "mapping_versions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[MappingStatus] = mapped_column(
        SQLEnum(MappingStatus), default=MappingStatus.DRAFT, nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    total_mappings: Mapped[int] = mapped_column(Integer, default=0)
    validated_mappings: Mapped[int] = mapped_column(Integer, default=0)
    failed_mappings: Mapped[int] = mapped_column(Integer, default=0)

    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_mapping_versions_project", "project_id"),
        Index(
            "ix_mapping_versions_version", "project_id", "version", unique=True
        ),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="mapping_versions")
    identity_mappings: Mapped[list["IdentityMapping"]] = relationship(
        "IdentityMapping",
        back_populates="mapping_version",
        cascade="all, delete-orphan",
    )


class IdentityMapping(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A single source-field to ontology-property identity mapping rule."""

    __tablename__ = "identity_mappings"

    mapping_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mapping_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    source_table: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_field: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    target_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_class_iri: Mapped[str] = mapped_column(String(500), nullable=False)
    target_property_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_property_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    key_type: Mapped[IdentityKeyType] = mapped_column(
        SQLEnum(IdentityKeyType),
        default=IdentityKeyType.CANDIDATE,
        nullable=False,
    )
    is_identity_key: Mapped[bool] = mapped_column(Boolean, default=False)

    transformation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    transformation_function: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )

    enum_mapping: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    unit_mapping: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    null_policy: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="draft")
    validation_status: Mapped[str] = mapped_column(String(50), default="pending")

    test_sample_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    test_success_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    test_failure_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    confidence: Mapped[float] = mapped_column(default=0.0)
    confidence_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_identity_mappings_version", "mapping_version_id"),
        Index(
            "ix_identity_mappings_target",
            "target_class_iri",
            "target_property_iri",
        ),
    )

    mapping_version: Mapped["MappingVersion"] = relationship(
        "MappingVersion", back_populates="identity_mappings"
    )


class DatasetSnapshot(Base, UUIDMixin, TimestampMixin):
    """A snapshot of the dataset produced under a mapping version."""

    __tablename__ = "dataset_snapshots"

    mapping_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mapping_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(50), nullable=False)

    storage_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    storage_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    object_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    link_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    source_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
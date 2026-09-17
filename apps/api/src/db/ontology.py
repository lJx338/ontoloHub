"""Ontology domain models ? Ontology, OntologyVersion, OntologyClass, Property, Relation, Constraint.

Includes a complete SHACL-subset constraint system (cardinality, value range, pattern, etc.).
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Enum as SQLEnum,
    JSON,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin

if TYPE_CHECKING:
    from .project import Project
    from .candidate import Proposal
    from .mapping import MappingVersion


# =====================================================================
# Enums
# =====================================================================


class OntologyKind(str, Enum):
    """Discriminator between project-private and shared reference ontologies."""

    PROJECT = "project"      # Project-private ontology
    REFERENCE = "reference"  # Cross-project reference (IOF, Schema.org, BFO, ...)


class OntologyStatus(str, Enum):
    """Lifecycle of an Ontology (overall, not per-version)."""

    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class OntologyVersionStatus(str, Enum):
    """Lifecycle of a specific OntologyVersion."""

    DRAFT = "draft"
    PUBLISHED = "published"
    LOCKED = "locked"
    ARCHIVED = "archived"


class ClassType(str, Enum):
    """Kind of ontology class entry."""

    ONTOLOGY_CLASS = "ontology_class"
    ENUM = "enum"


class PropertyType(str, Enum):
    """Kind of property entry (datatype vs object vs annotation)."""

    DATATYPE_PROPERTY = "datatype_property"
    OBJECT_PROPERTY = "object_property"
    ANNOTATION_PROPERTY = "annotation_property"


class RelationType(str, Enum):
    """Type of relation edge."""

    OBJECT = "object"
    HAS_MANY = "has_many"
    BELONGS_TO = "belongs_to"
    ASSOCIATES = "associates"


class ConstraintType(str, Enum):
    """Subset of SHACL constraint types we support."""

    CARDINALITY = "cardinality"
    QUALIFIED_CARDINALITY = "qualified_cardinality"
    ALL_VALUES_FROM = "all_values_from"
    SOME_VALUES_FROM = "some_values_from"
    HAS_VALUE = "has_value"
    MIN_EXCLUSIVE = "min_exclusive"
    MAX_EXCLUSIVE = "max_exclusive"
    MIN_INCLUSIVE = "min_inclusive"
    MAX_INCLUSIVE = "max_inclusive"
    PATTERN = "pattern"
    LENGTH = "length"


class SeverityLevel(str, Enum):
    """Severity of a constraint violation."""

    VIOLATION = "violation"   # Blocking
    WARNING = "warning"       # Advisory
    INFO = "info"             # Informational


# =====================================================================
# Ontology
# =====================================================================


class Ontology(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """An ontology container; kind='reference' means cross-project shared."""

    __tablename__ = "ontologies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    kind: Mapped[OntologyKind] = mapped_column(
        SQLEnum(OntologyKind, name="ontologykind"),
        default=OntologyKind.PROJECT,
        nullable=False,
        index=True,
    )

    status: Mapped[OntologyStatus] = mapped_column(
        SQLEnum(OntologyStatus, name="ontologystatus"),
        default=OntologyStatus.DRAFT,
        nullable=False,
    )

    version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    standard_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source_format: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    class_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    property_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_ontologies_namespace", "namespace"),
        Index("ix_ontologies_project", "project_id"),
        Index("ix_ontologies_kind_status", "kind", "status"),
    )

    project: Mapped[Optional["Project"]] = relationship(
        "Project", back_populates="ontologies"
    )
    versions: Mapped[list["OntologyVersion"]] = relationship(
        "OntologyVersion", back_populates="ontology", cascade="all, delete-orphan"
    )
    classes: Mapped[list["OntologyClass"]] = relationship(
        "OntologyClass", back_populates="ontology", cascade="all, delete-orphan"
    )
    properties: Mapped[list["Property"]] = relationship(
        "Property", back_populates="ontology", cascade="all, delete-orphan"
    )
    relations: Mapped[list["Relation"]] = relationship(
        "Relation", back_populates="ontology", cascade="all, delete-orphan"
    )


# =====================================================================
# OntologyVersion
# =====================================================================


class OntologyVersion(Base, UUIDMixin, TimestampMixin):
    """A snapshot version of an ontology ? recommended immutable once published."""

    __tablename__ = "ontology_versions"

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[OntologyVersionStatus] = mapped_column(
        SQLEnum(OntologyVersionStatus, name="ontologyversionstatus"),
        default=OntologyVersionStatus.DRAFT,
        nullable=False,
    )

    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_of: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    change_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    change_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Snapshot fields (serialized at publish time)
    class_snapshot: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    property_snapshot: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    relation_snapshot: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    constraint_snapshot: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index(
            "ix_ontology_versions_ontology_version",
            "ontology_id",
            "version",
            unique=True,
        ),
    )

    ontology: Mapped["Ontology"] = relationship("Ontology", back_populates="versions")


# =====================================================================
# OntologyClass
# =====================================================================


class OntologyClass(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A class entry in the ontology (TBox)."""

    __tablename__ = "ontology_classes"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    local_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    iri: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)

    class_type: Mapped[ClassType] = mapped_column(
        SQLEnum(ClassType, name="classtype"),
        default=ClassType.ONTOLOGY_CLASS,
        nullable=False,
    )

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=0)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    definition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    examples: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    enum_values: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    alignment: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    standard_mappings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_ontology_classes_ontology", "ontology_id"),
        Index("ix_ontology_classes_parent", "parent_id"),
        Index("ix_ontology_classes_level", "level"),
    )

    ontology: Mapped["Ontology"] = relationship("Ontology", back_populates="classes")
    parent: Mapped[Optional["OntologyClass"]] = relationship(
        "OntologyClass",
        remote_side="OntologyClass.id",
        back_populates="children",
    )
    children: Mapped[list["OntologyClass"]] = relationship(
        "OntologyClass", back_populates="parent"
    )
    properties: Mapped[list["Property"]] = relationship(
        "Property",
        back_populates="domain_class",
        cascade="all, delete-orphan",
        foreign_keys="Property.domain_id",
    )
    constraints: Mapped[list["Constraint"]] = relationship(
        "Constraint",
        back_populates="ontology_class",
        cascade="all, delete-orphan",
    )


# =====================================================================
# Property
# =====================================================================


class Property(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A property entry in the ontology (TBox)."""

    __tablename__ = "properties"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    local_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    iri: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)

    property_type: Mapped[PropertyType] = mapped_column(
        SQLEnum(PropertyType, name="propertytype"),
        default=PropertyType.DATATYPE_PROPERTY,
        nullable=False,
    )

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    domain_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    domain_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    range_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    range_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    range_class_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_multivalued: Mapped[bool] = mapped_column(Boolean, default=False)

    standard_mappings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_properties_ontology", "ontology_id"),
        Index("ix_properties_domain", "domain_id"),
        Index("ix_properties_type", "ontology_id", "property_type"),
    )

    ontology: Mapped["Ontology"] = relationship("Ontology", back_populates="properties")
    domain_class: Mapped[Optional["OntologyClass"]] = relationship(
        "OntologyClass", back_populates="properties", foreign_keys=[domain_id]
    )
    constraints: Mapped[list["Constraint"]] = relationship(
        "Constraint", back_populates="property", cascade="all, delete-orphan"
    )


# =====================================================================
# Relation
# =====================================================================


class Relation(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A relation template between classes (TBox)."""

    __tablename__ = "relations"

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    local_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    iri: Mapped[str] = mapped_column(String(500), nullable=False)

    relation_type: Mapped[RelationType] = mapped_column(
        SQLEnum(RelationType, name="relationtype"),
        default=RelationType.OBJECT,
        nullable=False,
    )

    source_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_class_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    target_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_class_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_transitive: Mapped[bool] = mapped_column(Boolean, default=False)
    is_symmetric: Mapped[bool] = mapped_column(Boolean, default=False)
    is_inverse_functional: Mapped[bool] = mapped_column(Boolean, default=False)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_relations_ontology", "ontology_id"),
        Index("ix_relations_source", "source_class_id"),
        Index("ix_relations_target", "target_class_id"),
    )

    ontology: Mapped["Ontology"] = relationship("Ontology", back_populates="relations")


# =====================================================================
# Constraint
# =====================================================================


class Constraint(Base, UUIDMixin, TimestampMixin):
    """A SHACL-subset constraint attached to a class or property."""

    __tablename__ = "constraints"

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    ontology_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="CASCADE"),
        nullable=True,
    )

    property_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=True,
    )

    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    constraint_type: Mapped[ConstraintType] = mapped_column(
        SQLEnum(ConstraintType, name="constrainttype"), nullable=False
    )

    severity: Mapped[SeverityLevel] = mapped_column(
        SQLEnum(SeverityLevel, name="severitylevel"),
        default=SeverityLevel.WARNING,
        nullable=False,
    )

    target_class_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    property_iri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_constraints_ontology", "ontology_id"),
        Index("ix_constraints_class", "ontology_class_id"),
        Index("ix_constraints_property", "property_id"),
        Index("ix_constraints_type", "ontology_id", "constraint_type"),
    )

    ontology_class: Mapped[Optional["OntologyClass"]] = relationship(
        "OntologyClass", back_populates="constraints"
    )
    property: Mapped[Optional["Property"]] = relationship(
        "Property", back_populates="constraints"
    )


__all__ = [
    # Enums
    "OntologyKind",
    "OntologyStatus",
    "OntologyVersionStatus",
    "ClassType",
    "PropertyType",
    "RelationType",
    "ConstraintType",
    "SeverityLevel",
    # Models
    "Ontology",
    "OntologyVersion",
    "OntologyClass",
    "Property",
    "Relation",
    "Constraint",
]
"""Object & Link models ? the business objects conforming to an ontology version."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, Any, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Enum as SQLEnum, JSON, Index
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin, ProjectMixin

if TYPE_CHECKING:
    from .project import Project
    from .mapping import MappingVersion, IdentityMapping
    from .ontology import OntologyClass, Property


class ObjectType(str, Enum):
    """Top-level object kind."""

    ENTITY = "entity"
    EVENT = "event"
    ACTIVITY = "activity"
    AGENT = "agent"
    PLACE = "place"
    DOCUMENT = "document"
    OTHER = "other"


class ObjectStatus(str, Enum):
    """Lifecycle of a single object record."""

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class LinkType(str, Enum):
    """Kind of link between two objects."""

    ASSOCIATION = "association"
    COMPOSITION = "composition"
    AGGREGATION = "aggregation"
    INHERITANCE = "inheritance"


class Object(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin, ProjectMixin):
    """A business object instance, conforming to a target ontology class."""

    __tablename__ = "objects"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ontology binding
    ontology_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_classes.id", ondelete="SET NULL"),
        nullable=True,
    )
    ontology_class_iri: Mapped[str] = mapped_column(String(500), nullable=False)
    ontology_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    object_type: Mapped[ObjectType] = mapped_column(
        SQLEnum(ObjectType), default=ObjectType.ENTITY, nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Object data (conforms to ontology properties via IdentityMapping rules)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Identity & lineage
    identity_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mapping_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    status: Mapped[ObjectStatus] = mapped_column(
        SQLEnum(ObjectStatus), default=ObjectStatus.ACTIVE, nullable=False
    )

    confidence: Mapped[float] = mapped_column(default=1.0)
    is_validated: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        Index("ix_objects_project", "project_id"),
        Index("ix_objects_class", "ontology_class_id"),
        Index("ix_objects_identity", "project_id", "identity_key"),
    )

    outgoing_links: Mapped[list["Link"]] = relationship(
        "Link",
        back_populates="source",
        cascade="all, delete-orphan",
        foreign_keys="Link.source_id",
    )
    incoming_links: Mapped[list["Link"]] = relationship(
        "Link", back_populates="target", foreign_keys="Link.target_id"
    )


class Link(Base, UUIDMixin, TimestampMixin, ProjectMixin):
    """A typed edge between two objects (relation instance)."""

    __tablename__ = "links"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    link_type: Mapped[LinkType] = mapped_column(
        SQLEnum(LinkType), default=LinkType.ASSOCIATION, nullable=False
    )

    ontology_relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ontology_relation_iri: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )

    identity_key: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True, index=True
    )

    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    confidence: Mapped[float] = mapped_column(default=1.0)
    is_validated: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        Index("ix_links_source", "source_id"),
        Index("ix_links_target", "target_id"),
        Index("ix_links_pair", "source_id", "target_id"),
    )

    source: Mapped["Object"] = relationship(
        "Object", back_populates="outgoing_links", foreign_keys=[source_id]
    )
    target: Mapped["Object"] = relationship(
        "Object", back_populates="incoming_links", foreign_keys=[target_id]
    )
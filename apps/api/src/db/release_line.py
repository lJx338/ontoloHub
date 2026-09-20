"""Release line models — branches + tags for ontology version lines (HIA-74 D4).

Like Git: a project has named branches per ontology (default ``main``); CRs are
authored against a source branch and merged into a target branch; tags are
immutable markers pointing to a specific OntologyVersion.

Models:

* ``OntologyBranch`` — a named line of ontology versions within a project.
  Each project+ontology pair has exactly one default branch (default ``main``).
* ``OntologyTag`` — an immutable marker pointing to a specific OntologyVersion.

These complement (not replace) the existing ``OntologyVersion`` table, which
remains the source of truth for ontology content snapshots.  Branches/tags
add the *lineage* layer (which version is "the latest on main" / which
versions are production release points).
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    Enum as SQLEnum,
    Index,
    UniqueConstraint,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin

if TYPE_CHECKING:
    from .project import Project
    from .ontology import Ontology, OntologyVersion


class MergeStrategy(str, Enum):
    """How a merge between branches is recorded."""

    FAST_FORWARD = "fast_forward"   # source is descendant of target → only move target head
    MERGE_COMMIT = "merge_commit"   # produce a merge commit carrying both parents
    NOOP = "noop"                   # source == target → nothing to do


# =====================================================================
# OntologyBranch
# =====================================================================


class OntologyBranch(Base, UUIDMixin, TimestampMixin):
    """A named branch of an Ontology within a project.

    Each (project_id, ontology_id, name) triple is unique.  Each pair
    ``(project_id, ontology_id)`` has exactly one ``is_default=True`` branch
    (enforced by a partial unique index in the Alembic migration; at the
    application layer we always default to ``main`` if no branch exists).

    ``head_version_id`` points to the tip ``OntologyVersion``; nullable until
    a version is published into the branch.
    """

    __tablename__ = "ontology_branches"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    head_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "ontology_id",
            "name",
            name="uq_ontology_branches_proj_ontology_name",
        ),
        Index(
            "ix_ontology_branches_proj_ontology",
            "project_id",
            "ontology_id",
        ),
    )

    project: Mapped["Project"] = relationship("Project")
    ontology: Mapped["Ontology"] = relationship("Ontology")
    head_version: Mapped[Optional["OntologyVersion"]] = relationship(
        "OntologyVersion", foreign_keys=[head_version_id]
    )


# =====================================================================
# OntologyTag
# =====================================================================


class OntologyTag(Base, UUIDMixin, TimestampMixin):
    """An immutable marker pointing to a specific OntologyVersion.

    Tags are conceptually immutable once created (mimicking Git tags).  We do
    not enforce immutability at the schema level (that would require
    triggers), but the API only exposes ``POST`` + ``GET`` + ``DELETE``;
    there is no ``PATCH version_id``.  Deleting a tag is allowed but
    discouraged (Git-style); the audit trail is preserved separately.
    """

    __tablename__ = "ontology_tags"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ontology_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontologies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "ontology_id",
            "name",
            name="uq_ontology_tags_proj_ontology_name",
        ),
        Index(
            "ix_ontology_tags_proj_ontology",
            "project_id",
            "ontology_id",
        ),
    )

    project: Mapped["Project"] = relationship("Project")
    ontology: Mapped["Ontology"] = relationship("Ontology")
    version: Mapped["OntologyVersion"] = relationship("OntologyVersion")


# =====================================================================
# BranchMerge — audit log of merge operations (HIA-74 §25.10 idea)
# =====================================================================


class BranchMerge(Base, UUIDMixin, TimestampMixin):
    """An audit record of a branch merge operation.

    Stores ``source_head_version_id`` + ``target_head_version_id`` (the
    parent versions involved) plus the resulting ``merge_version_id``
    (nullable for fast-forward, which reuses the source version).
    """

    __tablename__ = "branch_merges"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_head_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_head_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    merge_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="SET NULL"),
        nullable=True,
    )

    strategy: Mapped[MergeStrategy] = mapped_column(
        SQLEnum(MergeStrategy, name="mergestrategy"),
        default=MergeStrategy.FAST_FORWARD,
        nullable=False,
    )

    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    performed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_branch_merges_target_created", "target_branch_id", "created_at"),
    )


__all__ = [
    "MergeStrategy",
    "OntologyBranch",
    "OntologyTag",
    "BranchMerge",
]

"""Workspace / multi-tenant models — HIA-77 D1.

Workspace = the SaaS tenant boundary (one organization = one workspace).
Every ``Project`` belongs to at most one workspace; users join workspaces
through ``WorkspaceMembership``.  ``Project.workspace_id`` is nullable so
that legacy single-tenant projects keep working.

MVP scoping (D1):
  * Add ``workspaces`` + ``workspace_memberships`` tables
  * Add nullable ``workspace_id`` column on ``projects``
  * Enforce isolation at the API layer (not via separate DB schemas —
    that ships in D1.x).  ``X-Workspace-Id`` header lets a multi-workspace
    user pin the active tenant for a single request.
  * Quotas (``max_projects`` etc.) live on ``Workspace.settings`` JSON;
    the API rejects create calls when the cap is hit.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin

if TYPE_CHECKING:
    from .identity import User
    from .project import Project


# =====================================================================
# Enums
# =====================================================================


class WorkspacePlan(str, Enum):
    """Pricing plan / tier — drives quota enforcement."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class WorkspaceRole(str, Enum):
    """Role of a user within a workspace.

    Ordering is significant: ``rank()`` mirrors ``Role`` semantics.
    """

    OWNER = "owner"     # 全权：转让 workspace、删除 workspace
    ADMIN = "admin"     # 管理成员 + 创建/删除项目
    MEMBER = "member"   # 创建项目（在配额内）
    VIEWER = "viewer"   # 只读

    @classmethod
    def rank(cls, role: "WorkspaceRole") -> int:
        return _WORKSPACE_ROLE_RANK[role]


_WORKSPACE_ROLE_RANK: dict[WorkspaceRole, int] = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


def workspace_has_role(actual: Optional[WorkspaceRole], required: WorkspaceRole) -> bool:
    if actual is None:
        return False
    return WorkspaceRole.rank(actual) >= WorkspaceRole.rank(required)


# Default quota by plan.  Workspace.settings JSON may override per-tenant.
_DEFAULT_QUOTAS: dict[WorkspacePlan, dict[str, int]] = {
    WorkspacePlan.FREE: {
        "max_projects": 3,
        "max_users": 5,
        "max_objects": 10000,
    },
    WorkspacePlan.PRO: {
        "max_projects": 50,
        "max_users": 50,
        "max_objects": 1000000,
    },
    WorkspacePlan.ENTERPRISE: {
        "max_projects": 100000,
        "max_users": 100000,
        "max_objects": 100000000,
    },
}


def default_quota(plan: WorkspacePlan) -> dict[str, int]:
    """Return a copy of the default quota dict for ``plan``."""
    return dict(_DEFAULT_QUOTAS[plan])


# =====================================================================
# Workspace
# =====================================================================


class Workspace(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A SaaS tenant — one organization / company."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # URL-safe slug (lowercase alnum + hyphens); used in URLs like
    # ``ontolohub.com/{slug}/...``.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan: Mapped[WorkspacePlan] = mapped_column(
        String(50),
        default=WorkspacePlan.FREE.value,
        nullable=False,
    )

    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Settings keys:
    #   max_projects, max_users, max_objects (int) — quota overrides
    #   allow_public_catalog (bool) — expose ontology library publicly

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_workspaces_slug", "slug"),
        Index("ix_workspaces_owner", "owner_id"),
        Index("ix_workspaces_plan", "plan"),
    )

    memberships: Mapped[list["WorkspaceMembership"]] = relationship(
        "WorkspaceMembership", back_populates="workspace", cascade="all, delete-orphan"
    )
    projects: Mapped[list["Project"]] = relationship(
        "Project", back_populates="workspace"
    )

    # ---- quota helpers ----
    def effective_quota(self, key: str) -> int:
        """Return quota for ``key`` (max_projects / max_users / max_objects),
        falling back to the plan default if not overridden."""
        override = self.settings.get(key)
        if isinstance(override, int) and override >= 0:
            return override
        return default_quota(WorkspacePlan(self.plan)).get(key, 0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace {self.slug} plan={self.plan}>"


# =====================================================================
# WorkspaceMembership
# =====================================================================


class WorkspaceMembership(Base, UUIDMixin, TimestampMixin):
    """Binds a user to a workspace with a workspace-level role."""

    __tablename__ = "workspace_memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[WorkspaceRole] = mapped_column(
        String(50),
        default=WorkspaceRole.MEMBER.value,
        nullable=False,
    )
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="workspace_memberships")
    workspace: Mapped["Workspace"] = relationship(
        "Workspace", back_populates="memberships"
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "workspace_id", name="uq_workspace_memberships_user_workspace"
        ),
        Index("ix_workspace_memberships_workspace", "workspace_id"),
        Index("ix_workspace_memberships_user", "user_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<WorkspaceMembership user={self.user_id} "
            f"workspace={self.workspace_id} role={self.role}>"
        )


__all__ = [
    "Workspace",
    "WorkspaceMembership",
    "WorkspacePlan",
    "WorkspaceRole",
    "workspace_has_role",
    "default_quota",
]

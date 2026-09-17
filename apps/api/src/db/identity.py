"""Identity & access control models.

HIA-51 (M1-01) - 用户与项目成员关系 + 项目内角色 + 基础审计/隔离的入口。

设计原则：
- M0 单机原生：暂不引入 OIDC / JWT；通过 ``X-User-Email`` 或 ``X-User-Id``
  header 识别调用者（admin 引导后登录），与原生 demo 模式兼容。
- ``User`` 是全局账号；``Membership`` 把用户绑到项目并授予项目级 ``Role``。
- ``Role`` 序数：``viewer < reviewer < editor < owner``，权限判定走序数比较。
- ``AuditEvent`` 已经在 ``governance.py`` 里定义，是 append-only + 哈希链。

参见 MILESTONES.md §6 / M1-01（项目/成员/角色/需求、基础审计与隔离）与
PRD §3.13（基础隔离/审计）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, String, ForeignKey, Index, UniqueConstraint
from sqlalchemy import Uuid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin


class GlobalRole(str, Enum):
    """A user-wide role, independent of any single project."""

    ADMIN = "admin"          # 平台管理员（默认任何项目里都是 OWNER）
    USER = "user"            # 普通用户


class Role(str, Enum):
    """Per-project role. Ordering is significant for permission checks."""

    VIEWER = "viewer"        # 只读
    REVIEWER = "reviewer"    # 只读 + 评审/评论/批 CR
    EDITOR = "editor"        # VIEWER + REVIEWER + 编辑本体/映射/候选
    OWNER = "owner"          # 一切 + 管理成员

    @classmethod
    def rank(cls, role: "Role") -> int:
        return _ROLE_RANK[role]


_ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.REVIEWER: 1,
    Role.EDITOR: 2,
    Role.OWNER: 3,
}


def has_role(actual: Optional[Role], required: Role) -> bool:
    """Whether ``actual`` (or None) satisfies ``required``."""
    if actual is None:
        return False
    return Role.rank(actual) >= Role.rank(required)


class User(Base, UUIDMixin, TimestampMixin):
    """A user account. Minimal for M1 - email + display name + global role."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    global_role: Mapped[GlobalRole] = mapped_column(
        String(50),
        default=GlobalRole.USER.value,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    memberships: Mapped[list["Membership"]] = relationship(
        "Membership", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_email", "email"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} ({self.global_role})>"


class Membership(Base, UUIDMixin, TimestampMixin):
    """Bind a ``User`` to a ``Project`` with a per-project ``Role``."""

    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[Role] = mapped_column(
        String(50), default=Role.VIEWER.value, nullable=False
    )
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_memberships_user_project"),
        Index("ix_memberships_project", "project_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Membership user={self.user_id} project={self.project_id} role={self.role}>"

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
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import JSON, DateTime, String, ForeignKey, Index, UniqueConstraint
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

    # HIA-64 B1 — 密码 + JWT 认证。新装时未设密码的用户（bootstrap admin）仍允许
    # X-User-Email header 登录（向后兼容）；有密码的用户必须用 /auth/login。
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

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
    api_keys: Mapped[list["ApiKey"]] = relationship(
        "ApiKey", back_populates="user", cascade="all, delete-orphan"
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


class ApiKey(Base, UUIDMixin, TimestampMixin):
    """API Key for external integrations (HIA-64 B1).

    设计要点：
    - ``key_hash`` 存的是 SHA-256 哈希（**不存明文**）；明文只在 ``POST /api-keys``
      创建时一次性返回给用户。
    - ``scopes`` 是 list[str]，目前支持 ``read`` / ``write`` / ``connector`` /
      ``admin``（自由文本，便于扩展）。
    - ``project_id`` 为 NULL 时表示全局 key；非 NULL 时绑定项目，调用方自动
      获得该项目 OWNER 级别权限（用于 connector 等机器对机器场景）。
    - ``expires_at`` 是可选的硬过期；``revoked_at`` 是手动撤销时间。
    """

    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 哈希（64 字符 hex）。明文 key 只在创建响应中返回一次。
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # key 前 8 字符（如 ``ont_xxxxx``）用作识别 marker，便于审计 / 列表展示。
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="api_keys")

    __table_args__ = (
        Index("ix_api_keys_key_hash", "key_hash"),
        Index("ix_api_keys_user_project", "user_id", "project_id"),
    )

    @property
    def is_active(self) -> bool:
        """未撤销且未过期。"""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiKey {self.key_prefix}... user={self.user_id}>"

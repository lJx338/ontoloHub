"""Connector 模型 — 数据源连接器配置（HIA-71 + HIA-67）。

每种连接器类型（csv / postgres / json / parquet / excel）共享同一张表：
``connectors``。连接参数（host / port / db / user / password 等）放在
``config`` JSON 列，敏感字段在落盘前用对称加密（Fernet）封装。

``type`` 决定具体实现类（registry 在 ``src.services.connectors``）。
``last_status`` 与 ``last_tested_at`` 缓存最近一次连通性测试结果，避免
每个请求都跑真实查询。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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


class ConnectorType(str, Enum):
    """内置连接器类型。"""

    CSV = "csv"
    EXCEL = "excel"
    JSON = "json"
    PARQUET = "parquet"
    POSTGRESQL = "postgresql"
    # M2+ 扩展
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    REDSHIFT = "redshift"
    MYSQL = "mysql"
    MONGODB = "mongodb"
    REST_API = "rest_api"


class ConnectorStatus(str, Enum):
    """连接器生命周期状态。"""

    ACTIVE = "active"          # 可用
    DISABLED = "disabled"      # 主动停用
    ERROR = "error"            # 最近测试失败
    ARCHIVED = "archived"      # 已删除


class Connector(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """一个项目下的连接器实例。

    - ``type`` 决定实现；
    - ``config`` JSON 列按类型承载不同字段（如 postgres 需要 host / port /
      database / username / password；csv 需要 file_path）；
    - ``secret_fields`` 列出 config 中**已加密**的字段名，便于 UI 屏蔽。
    """

    __tablename__ = "connectors"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    type: Mapped[ConnectorType] = mapped_column(
        SQLEnum(ConnectorType), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 自由 JSON 配置（按 connector type 不同 schema）
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # config 中被加密字段的列表（方便 UI 标记 / 屏蔽）
    secret_fields: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )

    status: Mapped[ConnectorStatus] = mapped_column(
        SQLEnum(ConnectorStatus),
        default=ConnectorStatus.ACTIVE,
        nullable=False,
    )

    last_tested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # 可选：默认取数限制 / 抽样规则
    default_sample_limit: Mapped[int] = mapped_column(default=1000, nullable=False)

    __table_args__ = (
        Index("ix_connectors_project", "project_id"),
        Index("ix_connectors_project_type", "project_id", "type"),
    )

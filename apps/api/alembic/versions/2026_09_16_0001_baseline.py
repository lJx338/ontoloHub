"""baseline schema

首次 upgrade 时落地当前 SQLAlchemy 元数据对应的完整 schema。

后续 schema 改动通过 ``alembic revision --autogenerate -m "..."`` 生成新迁移。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-16 13:00:00.000000

"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建所有业务表（target schema）。"""
    # 触发 SQLAlchemy 完整建表
    from src.db.base import Base
    from src.db import models  # noqa: F401  仅用于模型注册

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """删除所有业务表（请谨慎）。"""
    from src.db.base import Base
    from src.db import models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())

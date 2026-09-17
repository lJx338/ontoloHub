"""Alembic environment configuration.

使用 SQLAlchemy 同步引擎跑迁移；从 src.core.config.settings 读取 database URL。
target_metadata 来自 src.db.base.Base，便于配合 ``alembic revision --autogenerate``。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import 项目内模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import settings  # noqa: E402
from src.db.base import Base  # noqa: E402
from src.db import models  # noqa: F401, E402  # 导入所有模型注册到 Base.metadata

config = context.config

# 注入数据库 URL（env.py 优先级高于 alembic.ini）。
# Alembic 走同步引擎，剥掉 async 驱动前缀（+asyncpg / +aiosqlite）。
_db_url = settings.database.url
for _async_drv in ("+asyncpg", "+aiosqlite"):
    _db_url = _db_url.replace(_async_drv, "")
config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline 模式：仅生成 SQL。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online 模式：连接数据库实跑迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

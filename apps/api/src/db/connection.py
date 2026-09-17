"""Database connection management."""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings


# Async engine (used by FastAPI runtime)
_async_kwargs: dict = {
    "echo": settings.database.echo,
    "pool_pre_ping": True,
}
if not settings.database.url.startswith("sqlite"):
    _async_kwargs.update(
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
    )

async_engine = create_async_engine(settings.database.url, **_async_kwargs)

# Async session factory
async_session_factory = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Sync engine (used by Alembic migrations)
_sync_url = settings.database.url
for _drv in ("+asyncpg", "+aiosqlite"):
    _sync_url = _sync_url.replace(_drv, "")
_sync_kwargs: dict = {"echo": settings.database.echo}
if not _sync_url.startswith("sqlite"):
    _sync_kwargs.update(
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_pre_ping=True,
    )

sync_engine = create_engine(_sync_url, **_sync_kwargs)

# Sync session factory
sync_session_factory = sessionmaker(
    sync_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session for a request."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Async session context manager for non-request code paths."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Startup hook.

    Schema is managed by Alembic (``alembic upgrade head``); here we only
    ping the connection so the lifespan remains meaningful.
    """
    try:
        async with async_engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        # M0 tolerates DB unavailability at startup; Alembic owns the schema.
        pass


async def close_db() -> None:
    """Dispose of async engine on shutdown."""
    await async_engine.dispose()
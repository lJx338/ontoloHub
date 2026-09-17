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

from src.core.config import get_settings


def _build_async_engine():
    """Construct the async engine from the *current* settings.

    Called per-request (factory) so that tests can monkeypatch
    ``DATABASE_URL`` and observe the change. The engine/session
    instances themselves are kept in module-level globals so that
    FastAPI dependencies and Alembic share a single pool.
    """
    s = get_settings()
    url = s.database.url
    kwargs: dict = {
        "echo": s.database.echo,
        "pool_pre_ping": True,
    }
    if not url.startswith("sqlite"):
        kwargs.update(
            pool_size=s.database.pool_size,
            max_overflow=s.database.max_overflow,
        )
    return create_async_engine(url, **kwargs)


async_engine = _build_async_engine()

# Async session factory
async_session_factory = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


def _build_sync_engine():
    s = get_settings()
    url = s.database.url
    for drv in ("+asyncpg", "+aiosqlite"):
        url = url.replace(drv, "")
    kwargs: dict = {"echo": s.database.echo}
    if not url.startswith("sqlite"):
        kwargs.update(
            pool_size=s.database.pool_size,
            max_overflow=s.database.max_overflow,
            pool_pre_ping=True,
        )
    return create_engine(url, **kwargs)


sync_engine = _build_sync_engine()

# Sync session factory
sync_session_factory = sessionmaker(
    sync_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def reinit_engines() -> None:
    """Dispose the current engines and re-create them from current settings.

    Tests call this after ``monkeypatch.setenv('DATABASE_URL', ...)`` to
    pick up a new sqlite path. Production code shouldn't call this.
    """
    global async_engine, async_session_factory, sync_engine, sync_session_factory
    try:
        await async_engine.dispose()
    except Exception:
        pass
    try:
        sync_engine.dispose()
    except Exception:
        pass
    async_engine = _build_async_engine()
    async_session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    sync_engine = _build_sync_engine()
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
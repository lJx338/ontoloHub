import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\qiushi\ontoloHub")
sys.path.insert(0, str(ROOT / "apps" / "api"))

import asyncio

async def main():
    db_path = Path(tempfile.gettempdir()) / f"_dbtest_{os.getpid()}.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    print(f"db_path: {db_path}")
    print(f"env: {os.environ['DATABASE_URL']}")

    # Clear cache and reinit
    from src.core.config import get_settings
    get_settings.cache_clear()
    print(f"settings.database.url: {get_settings().database.url}")

    from src.db import connection as conn
    print(f"before reinit async_engine.url: {conn.async_engine.url}")
    print(f"before reinit sync_engine.url: {conn.sync_engine.url}")
    await conn.reinit_engines()
    print(f"after reinit async_engine.url: {conn.async_engine.url}")
    print(f"after reinit sync_engine.url: {conn.sync_engine.url}")

    # Run alembic upgrade on sync engine
    from alembic import command
    from alembic.config import Config
    alembic_cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    sync_url = f"sqlite:///{db_path}"
    print(f"alembic sync_url: {sync_url}")
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    cwd_save = os.getcwd()
    try:
        os.chdir(ROOT / "apps" / "api")
        command.upgrade(alembic_cfg, "head")
    finally:
        os.chdir(cwd_save)

    print(f"db file exists: {db_path.exists()}, size: {db_path.stat().st_size}")

    # Now ensure admin via async
    from src.api.auth import ensure_bootstrap_admin
    async with conn.async_session_factory() as s:
        await ensure_bootstrap_admin()

    # Check users count
    from sqlalchemy import text
    async with conn.async_session_factory() as s:
        result = await s.execute(text("SELECT count(*) FROM users"))
        print(f"users count after bootstrap: {result.scalar()}")

asyncio.run(main())

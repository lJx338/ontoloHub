import os, sys, tempfile
from pathlib import Path

ROOT = Path(r"D:\qiushi\ontoloHub")
sys.path.insert(0, str(ROOT / "apps" / "api"))

import asyncio

async def main():
    db_path = Path(tempfile.gettempdir()) / f"_dbtest_{os.getpid()}.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    # Clear cache
    from src.core.config import get_settings
    get_settings.cache_clear()

    # Reinit engines
    from src.db import connection as conn
    await conn.reinit_engines()

    # Run alembic
    from alembic import command
    from alembic.config import Config
    alembic_cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    sync_url = f"sqlite:///{db_path}"
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    cwd_save = os.getcwd()
    try:
        os.chdir(ROOT / "apps" / "api")
        command.upgrade(alembic_cfg, "head")
    finally:
        os.chdir(cwd_save)

    # Check what's in the DB
    import sqlite3
    c = sqlite3.connect(str(db_path))
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"tables: {tables}")
    print(f"users rowcount: {c.execute('SELECT count(*) FROM users').fetchone()}")
    c.close()

asyncio.run(main())

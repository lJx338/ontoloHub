import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\qiushi\ontoloHub")
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import select


async def main():
    db_path = tempfile.mktemp(suffix=".db")
    # 强制清理（上次跑残留）
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    sync_url = f"sqlite:///{db_path}"
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    cwd_save = os.getcwd()
    try:
        os.chdir(ROOT / "apps" / "api")
        command.upgrade(alembic_cfg, "head")
    finally:
        os.chdir(cwd_save)

    from src.db.connection import async_session_factory
    from src.api.auth import ensure_bootstrap_admin
    from src.db.governance import AuditEvent

    async with async_session_factory() as session:
        await ensure_bootstrap_admin()

    # Make project + use_case + requirement like the test does
    from httpx import ASGITransport, AsyncClient
    from src.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"X-User-Email": "alice@x.com", "X-User-Name": "alice"}
        p = (await c.post("/projects", json={"name": "P"}, headers=h)).json()
        await c.post(f"/projects/{p['id']}/use-cases", json={"name": "UC1"}, headers=h)
        await c.post(f"/projects/{p['id']}/requirements", json={"name": "R1"}, headers=h)

    async with async_session_factory() as session:
        events = (await session.execute(select(AuditEvent).order_by(AuditEvent.created_at.asc()))).scalars().all()
        for e in events:
            print(f"\n=== Event {e.id}")
            print(f"event_type value = {getattr(e.event_type, 'value', None)}, type = {type(e.event_type).__name__}")
            print(f"before = {e.before}")
            print(f"after  = {e.after}")
            print(f"prev_hash  = {e.prev_hash[:8] if e.prev_hash else None}")
            print(f"entry_hash = {e.entry_hash[:8] if e.entry_hash else None}")


asyncio.run(main())

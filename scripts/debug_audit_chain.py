"""调试 audit hash chain 的脚本：复用测试的 isolated_app fixture 的设置。"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from src.db.connection import async_session_factory
from src.db.governance import AuditEvent


async def main():
    db_path = Path(tempfile.gettempdir()) / f"audit_debug_{int(time.time()*1000)}.db"
    if db_path.exists():
        db_path.unlink()
    print(f"DB: {db_path}")

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    # 重新创建 engine
    from src.db import connection as conn_module
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine

    # dispose old engines
    try:
        await conn_module.async_engine.dispose()
        conn_module.sync_engine.dispose()
    except Exception:
        pass

    # re-create
    new_async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    conn_module.async_engine = create_async_engine(
        new_async_url, echo=False, pool_pre_ping=True
    )
    conn_module.async_session_factory = async_sessionmaker(
        conn_module.async_engine,
        class_=conn_module.AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    conn_module.sync_engine = create_engine(sync_url, echo=False)
    conn_module.sync_session_factory = sessionmaker(
        conn_module.sync_engine,
        expire_on_commit=False,
        autoflush=False,
    )

    # alembic upgrade
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

    # bootstrap
    from src.api.main import app
    from src.api.auth import ensure_bootstrap_admin
    async with async_session_factory() as s:
        await ensure_bootstrap_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"X-User-Email": "alice@x.com", "X-User-Name": "alice"}
        p = (await c.post("/projects", json={"name": "P"}, headers=h)).json()
        print(f"project: {p['id']}")
        uc = (await c.post(f"/projects/{p['id']}/use-cases", json={"name": "UC1"}, headers=h))
        print(f"use_case status: {uc.status_code}")
        req = (await c.post(f"/projects/{p['id']}/requirements", json={"name": "R1"}, headers=h))
        print(f"requirement status: {req.status_code}")

    # Dump events
    async with async_session_factory() as session:
        events = (await session.execute(
            select(AuditEvent).order_by(AuditEvent.created_at.asc())
        )).scalars().all()
        print(f"\n=== {len(events)} audit events ===")
        for i, e in enumerate(events):
            et = e.event_type.value if hasattr(e.event_type, "value") else e.event_type
            print(f"\nEvent #{i} id={e.id}")
            print(f"  event_type = {et!r}")
            print(f"  target_type = {e.target_type!r}, target_label = {e.target_label!r}")
            print(f"  before keys = {list(e.before.keys()) if e.before else None}")
            print(f"  after  keys = {list(e.after.keys()) if e.after else None}")
            print(f"  prev_hash  = {e.prev_hash[:16] if e.prev_hash else None}...")
            print(f"  entry_hash = {e.entry_hash[:16] if e.entry_hash else None}...")

    # Verify chain
    from src.api.audit import verify_chain
    from src.api.auth import get_current_user, CurrentPrincipal
    from src.db.identity import User, GlobalRole
    # Get admin user
    async with async_session_factory() as session:
        admin = (await session.execute(select(User).where(User.email == "admin@ontolohub.local"))).scalar_one()
    principal = CurrentPrincipal(user=admin, is_admin=True)

    # 直接调用 verify_chain 的方法（不通过 HTTP）
    async with async_session_factory() as session:
        from src.db.governance import AuditEvent as AE
        events = (await session.execute(
            select(AE).order_by(AE.created_at.asc(), AE.id.asc())
        )).scalars().all()

        prev_hash = None
        checked = 0
        import hashlib
        import json
        for e in events:
            def _canon(p):
                return json.dumps(p, sort_keys=True, separators=(",", ":"), default=str)
            payload = {
                "id": str(e.id),
                "event_type": e.event_type.value if hasattr(e.event_type, "value") else e.event_type,
                "actor_id": str(e.actor_id) if e.actor_id else None,
                "project_id": str(e.project_id) if e.project_id else None,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "prev_hash": prev_hash,
                "before": e.before,
                "after": e.after,
            }
            digest = hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()
            match_prev = e.prev_hash == prev_hash
            match_entry = e.entry_hash == digest
            print(f"\n--- Checking event #{checked} (id={e.id})")
            print(f"    prev_hash match: {match_prev} | stored={e.prev_hash[:16] if e.prev_hash else None} want={prev_hash[:16] if prev_hash else None}")
            print(f"    entry_hash match: {match_entry}")
            if not match_entry:
                print(f"    digest     = {digest[:16]}...")
                print(f"    entry_hash = {e.entry_hash[:16] if e.entry_hash else None}...")
            prev_hash = e.entry_hash
            checked += 1
            if not (match_prev and match_entry):
                print(f"    ❌ BROKEN at #{checked}")
                break


asyncio.run(main())

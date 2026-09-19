"""Tests for HIA-90 D5 — Backup & Disaster Recovery.

Covers the round-trip SQLite backup → restore → verify, file component
backup, config component backup, scheduler default-schedule creation,
expiry sweep, DR drill, and the admin API endpoints.

Each test uses an isolated SQLite + alembic up head + ASGI transport,
matching the pattern in test_release_line.py / test_auth_jwt.py.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
import sys
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))


# ---------------------------------------------------------------------------
# Fixtures: per-test isolated SQLite + ASGI client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # Backup storage/work/upload dirs
    storage = tmp_path / "backups"
    work = tmp_path / "work"
    uploads = tmp_path / "uploads"
    exports = tmp_path / "exports"
    for p in (storage, work, uploads, exports):
        p.mkdir(exist_ok=True)

    monkeypatch.setenv("BACKUP_STORAGE_DIR", str(storage))
    monkeypatch.setenv("BACKUP_WORK_DIR", str(work))
    monkeypatch.setenv("BACKUP_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv("BACKUP_EXPORTS_DIR", str(exports))
    monkeypatch.setenv("BACKUP_SCHEDULE_ENABLED", "false")

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    from src.db import connection as conn
    await conn.reinit_engines()
    async_session_factory = conn.async_session_factory

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

    from src.api.main import app
    from src.api.auth import ensure_bootstrap_admin

    async with async_session_factory() as s:
        await ensure_bootstrap_admin()

    # Also create a non-admin user so we can test role gating.
    from src.db.identity import User, GlobalRole
    from sqlalchemy.ext.asyncio import AsyncSession

    async with async_session_factory() as s:
        member = User(
            email="member@example.com",
            display_name="Member",
            global_role=GlobalRole.USER.value,
        )
        s.add(member)
        await s.flush()
        member_id = str(member.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield {
            "app": app,
            "client": client,
            "db_path": db_path,
            "storage": storage,
            "work": work,
            "uploads": uploads,
            "exports": exports,
            "member_id": member_id,
        }


@pytest_asyncio.fixture
async def client(isolated_app):
    return isolated_app["client"]


@pytest_asyncio.fixture
async def session_factory(isolated_app):
    from src.db import connection as conn
    return conn.async_session_factory


@pytest_asyncio.fixture
async def manager(isolated_app, monkeypatch):
    from src.services.backup import BackupManager
    from src.core import config as cfg

    # Make sure settings pick up the env vars we set in isolated_app
    cfg.get_settings.cache_clear()

    monkeypatch.setattr(
        cfg.settings.backup, "storage_dir", str(isolated_app["storage"]),
    )
    monkeypatch.setattr(
        cfg.settings.backup, "work_dir", str(isolated_app["work"]),
    )
    monkeypatch.setattr(
        cfg.settings.backup, "uploads_dir", str(isolated_app["uploads"]),
    )
    monkeypatch.setattr(
        cfg.settings.backup, "exports_dir", str(isolated_app["exports"]),
    )
    monkeypatch.setattr(cfg.settings.backup, "schedule_enabled", False)

    return BackupManager(
        storage_dir=isolated_app["storage"],
        work_dir=isolated_app["work"],
        encryption_key="",
    )


@pytest_asyncio.fixture
async def populated_sqlite(isolated_app):
    """Create a throwaway SQLite file, populate a couple of tables."""
    db = isolated_app["db_path"].parent / "fixture.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(5):
            conn.execute("INSERT INTO t (val) VALUES (?)", (f"row{i}",))
        conn.commit()
    finally:
        conn.close()
    return db


@pytest_asyncio.fixture
async def files_seed(isolated_app):
    up = isolated_app["uploads"]
    ex = isolated_app["exports"]
    (up / "a.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (up / "b.txt").write_text("hello", encoding="utf-8")
    (ex / "c.ttl").write_text("@prefix : <#> .\n:s p :o .\n", encoding="utf-8")
    return {"uploads": up, "exports": ex}


# ---------------------------------------------------------------------------
# Low-level component tests
# ---------------------------------------------------------------------------


def test_sqlite_url_parser_handles_relative():
    from src.services.backup import _sqlite_path_from_url
    url = "sqlite:///./data/ontolohub.db"
    p = _sqlite_path_from_url(url)
    assert p is not None
    assert p.name == "ontolohub.db"


async def test_database_component_round_trip(manager, populated_sqlite, monkeypatch):
    """The database component must be a gzipped SQLite snapshot that
    decodes back to a valid file we can query."""
    from src.core.config import settings
    from src.services.backup import BackupComponent

    # Point the manager at the throwaway db.
    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")

    backup_dir = manager.storage_dir / "db-test"
    backup_dir.mkdir()
    component = BackupComponent(name="database")
    await manager._backup_database(backup_dir, component)

    gz = backup_dir / "db.sqlite3.gz"
    assert gz.exists()
    assert component.size_bytes > 0
    assert component.checksum and len(component.checksum) == 64

    # Decompress and query — data must survive.
    out = manager.work_dir / "decompressed.sqlite3"
    with gzip.open(gz, "rb") as f:
        out.write_bytes(f.read())
    conn = sqlite3.connect(str(out))
    try:
        rows = conn.execute("SELECT val FROM t ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [(f"row{i}",) for i in range(5)]


async def test_files_component_packs_uploads_and_exports(manager, files_seed):
    from src.services.backup import BackupComponent
    backup_dir = manager.storage_dir / "files-test"
    backup_dir.mkdir()
    component = BackupComponent(name="files")
    await manager._backup_files(backup_dir, component)

    tar = backup_dir / "files.tar.gz"
    assert tar.exists() and component.size_bytes > 0

    with tarfile.open(tar, "r:gz") as t:
        names = {m.name for m in t.getmembers()}
    assert any(n.endswith("a.csv") for n in names)
    assert any(n.endswith("c.ttl") for n in names)


async def test_config_component_captures_alembic_and_env(manager):
    from src.services.backup import BackupComponent

    # Touch a fake .env at repo root to test the "present" branch
    repo_root = ROOT
    env_path = repo_root / ".env"
    created = False
    if not env_path.is_file():
        env_path.write_text("KEY=value\n", encoding="utf-8")
        created = True
    try:
        backup_dir = manager.storage_dir / "config-test"
        backup_dir.mkdir()
        component = BackupComponent(name="config")
        await manager._backup_config(backup_dir, component)

        tar = backup_dir / "config.tar.gz"
        assert tar.exists() and component.size_bytes > 0

        # manifest inside the staging captured tables.txt + alembic_head.txt
        with tarfile.open(tar, "r:gz") as t:
            names = {m.name for m in t.getmembers()}
        assert any(n.endswith("tables.txt") for n in names)
        assert any(n.endswith("env_presence.txt") for n in names)
    finally:
        if created:
            env_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# End-to-end manager.create_backup
# ---------------------------------------------------------------------------


async def test_create_full_backup_then_verify_and_restore(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind
    from src.db.backup import BackupStatus

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")

    async with session_factory() as session:
        backup = await manager.create_backup(
            session,
            BackupKind.FULL,
            triggered_by="test",
            retention_days=7,
            notes="pytest full",
        )
        await session.commit()

        assert backup.status == BackupStatus.SUCCESS
        assert backup.size_bytes > 0
        assert backup.checksum
        assert backup.expires_at is not None
        assert Path(backup.storage_path).exists()

        # Verify
        ok, report = await manager.verify_backup(session, backup.id)
        assert ok, report
        assert report["manifest_present"]
        assert report["checksum_ok"] is True

        # Dry-run restore
        log = await manager.restore_backup(session, backup.id, dry_run=True)
        await session.commit()
        assert log.status.value == "success"
        assert log.dry_run is True
        assert log.restored_metadata
        assert log.restored_metadata["listing"]


async def test_incremental_backup_skips_database(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.INCREMENTAL,
            triggered_by="test", parent_backup_id=None,
        )
        await session.commit()
        assert backup.status.value == "success"
        assert "database" not in backup.backup_metadata["components"]
        component_names = {
            c["name"] for c in backup.backup_metadata["components"]
        }
        assert "files" in component_names


# ---------------------------------------------------------------------------
# Verify: corrupt detection
# ---------------------------------------------------------------------------


async def test_verify_flags_missing_manifest(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test",
        )
        await session.commit()

        (Path(backup.storage_path) / "manifest.json").unlink()
        ok, report = await manager.verify_backup(session, backup.id)
        assert ok is False
        assert not report["manifest_present"]


async def test_verify_flags_checksum_mismatch(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test",
        )
        await session.commit()

        p = Path(backup.storage_path) / "files.tar.gz"
        p.write_bytes(b"corrupted content")
        ok, report = await manager.verify_backup(session, backup.id)
        assert ok is False
        assert report["checksum_ok"] is False


# ---------------------------------------------------------------------------
# Restore behaviour
# ---------------------------------------------------------------------------


async def test_restore_non_dry_run_requires_target_dir(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind, BackupRestoreError

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test",
        )
        await session.commit()
        with pytest.raises(BackupRestoreError):
            await manager.restore_backup(
                session, backup.id, dry_run=False, target_dir=None,
            )


async def test_restore_non_dry_run_copies_into_target(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite, tmp_path,
):
    from src.core.config import settings
    from src.services.backup import BackupKind

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test",
        )
        await session.commit()

        target = tmp_path / "restore-target"
        target.mkdir()
        log = await manager.restore_backup(
            session, backup.id,
            dry_run=False,
            target_dir=target,
            components=["files"],
        )
        await session.commit()
        assert log.status.value == "success"
        assert (target / "files").exists()


# ---------------------------------------------------------------------------
# Encryption round-trip
# ---------------------------------------------------------------------------


async def test_encrypted_backup_decrypts_on_restore(
    session_factory, isolated_app, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupManager, BackupKind

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    mgr = BackupManager(
        storage_dir=isolated_app["storage"],
        work_dir=isolated_app["work"],
        encryption_key="super-secret-passphrase",
    )
    async with session_factory() as session:
        backup = await mgr.create_backup(
            session, BackupKind.DATABASE, triggered_by="test",
        )
        await session.commit()
        assert backup.is_encrypted is True
        assert (Path(backup.storage_path) / "backup.enc").exists()

        log = await mgr.restore_backup(session, backup.id, dry_run=True)
        await session.commit()
        assert log.status.value == "success"


async def test_encrypted_backup_fails_without_key(
    session_factory, isolated_app, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupManager, BackupKind, BackupRestoreError

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    mgr = BackupManager(
        storage_dir=isolated_app["storage"],
        work_dir=isolated_app["work"],
        encryption_key="secret-key",
    )
    async with session_factory() as session:
        backup = await mgr.create_backup(
            session, BackupKind.DATABASE, triggered_by="test",
        )
        await session.commit()
        assert backup.is_encrypted is True

        naked = BackupManager(
            storage_dir=isolated_app["storage"],
            work_dir=isolated_app["work"],
            encryption_key="",
        )
        with pytest.raises(BackupRestoreError):
            await naked.restore_backup(session, backup.id, dry_run=True)


# ---------------------------------------------------------------------------
# Scheduler defaults
# ---------------------------------------------------------------------------


async def test_default_schedules_seeded_once(session_factory, monkeypatch):
    from src.core.config import settings
    from src.db.backup import BackupSchedule
    from src.services.backup_scheduler import ensure_default_schedules

    monkeypatch.setattr(settings.backup, "schedule_enabled", False)

    async with session_factory() as session:
        await ensure_default_schedules(session)
        await ensure_default_schedules(session)  # idempotent
        rows = (await session.execute(
            select(BackupSchedule).order_by(BackupSchedule.name),
        )).scalars().all()
        names = {r.name for r in rows}
        assert "daily-full" in names
        assert "every-6h-incremental" in names


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------


async def test_expire_old_backups_flags_past_expires_at(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind
    from src.db.backup import BackupStatus

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test", retention_days=1,
        )
        await session.commit()

        backup.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.flush()

        expired = await manager.expire_old_backups(session)
        assert backup.id in expired
        await session.refresh(backup)
        assert backup.status == BackupStatus.EXPIRED


# ---------------------------------------------------------------------------
# DR drill
# ---------------------------------------------------------------------------


async def test_dr_drill_picks_latest_success_and_reports(
    session_factory, manager, files_seed, monkeypatch, populated_sqlite,
):
    from src.core.config import settings
    from src.services.backup import BackupKind
    from src.db.backup import DrDrillStatus

    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")
    async with session_factory() as session:
        backup = await manager.create_backup(
            session, BackupKind.FULL, triggered_by="test",
        )
        await session.commit()

        drill = await manager.run_dr_drill(session, backup_id=backup.id)
        await session.commit()
        assert drill.status == DrDrillStatus.SUCCESS
        assert drill.checklist["verify_ok"]
        assert drill.rto_seconds is not None
        assert drill.rpo_seconds is not None


async def test_dr_drill_without_backup_fails(session_factory, manager):
    from src.services.backup import BackupError
    async with session_factory() as session:
        with pytest.raises(BackupError):
            await manager.run_dr_drill(session, backup_id=None)


# ---------------------------------------------------------------------------
# API endpoints (admin only)
# ---------------------------------------------------------------------------


async def test_api_create_list_get_verify_restore_health(
    client, manager, files_seed, monkeypatch, populated_sqlite, isolated_app,
):
    from src.core.config import settings
    monkeypatch.setattr(settings.database, "url", f"sqlite:///{populated_sqlite}")

    # The bootstrap admin must satisfy is_global_admin
    r = await client.post(
        "/api/admin/backups",
        json={"kind": "full", "retention_days": 7, "notes": "api-test"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    bid = body["id"]
    assert body["status"] == "success"
    assert body["is_encrypted"] is False

    # List
    r = await client.get("/api/admin/backups")
    assert r.status_code == 200
    assert any(b["id"] == bid for b in r.json())

    # Get
    r = await client.get(f"/api/admin/backups/{bid}")
    assert r.status_code == 200
    assert r.json()["id"] == bid

    # Verify
    r = await client.post(f"/api/admin/backups/{bid}/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Restore dry-run
    r = await client.post(
        f"/api/admin/backups/{bid}/restore",
        json={"dry_run": True},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success"

    # Health
    r = await client.get("/api/admin/health/backup")
    assert r.status_code == 200
    h = r.json()
    assert h["healthy"] is True
    assert h["pending_running"] == 0

    # Schedules
    r = await client.get("/api/admin/backup-schedules")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "daily-full" in names

    # DR drill
    r = await client.post(f"/api/admin/dr-drill?backup_id={bid}")
    assert r.status_code == 201, r.text
    drill = r.json()
    assert drill["status"] == "success"
    assert drill["rto_seconds"] is not None

    # Cleanup
    r = await client.delete(f"/api/admin/backups/{bid}")
    assert r.status_code == 204


async def test_api_create_requires_global_admin(client, isolated_app):
    """Non-admin caller (member) gets 403.

    Member user has global_role=USER so the auth dependency must deny.
    """
    r = await client.post(
        "/api/admin/backups",
        json={"kind": "full"},
        headers={"X-User-Email": "member@example.com"},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Schema-level: enum values are stable
# ---------------------------------------------------------------------------


def test_backup_kind_enum_values():
    from src.db.backup import BackupKind
    assert {k.value for k in BackupKind} == {
        "full", "database", "files", "config", "incremental",
    }


def test_backup_status_enum_values():
    from src.db.backup import BackupStatus
    assert {s.value for s in BackupStatus} == {
        "pending", "running", "success", "failed", "expired", "corrupted",
    }


def test_schedule_kind_enum_values():
    from src.db.backup import ScheduleKind
    assert {k.value for k in ScheduleKind} == {"full", "incremental", "drill"}

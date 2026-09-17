"""Connector API 集成测试（HIA-71 B6）。

走 httpx AsyncClient（ASGITransport），
覆盖 list / create / get / patch / delete / test / tables / snapshot。
"""
from __future__ import annotations

import csv
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # → D:\qiushi\ontoloHub
import sys
sys.path.insert(0, str(ROOT / "apps" / "api"))

from src.db.connection import async_session_factory
from src.db.identity import User
from src.db.project import Project, ProjectStatus
from src.db.identity import Membership, Role


# ---------------------------------------------------------------------------
# DB isolation fixtures（与 test_auth_isolation_audit.py 一致）
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    from src.db import connection as conn
    await conn.reinit_engines()
    async_session_factory_ = conn.async_session_factory

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

    async with async_session_factory_() as s:
        await ensure_bootstrap_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client, async_session_factory_


@pytest_asyncio.fixture
async def client(isolated_app):
    _, client, _ = isolated_app
    return client


@pytest_asyncio.fixture
async def project_and_admin(isolated_app):
    """Returns (project_id, admin_user_id)."""
    _, client, factory = isolated_app
    async with factory() as session:
        res = await session.execute(select(User).where(User.email == "admin@ontolohub.local"))
        admin = res.scalar_one()
        p = Project(
            name=f"test-{uuid.uuid4().hex[:8]}",
            status=ProjectStatus.DISCOVERY,
            created_by=admin.id,
            owner_id=admin.id,
        )
        session.add(p)
        await session.flush()
        await session.refresh(p)
        m = Membership(
            user_id=admin.id,
            project_id=p.id,
            role=Role.OWNER.value,
            invited_by=admin.id,
            is_active=True,
        )
        session.add(m)
        await session.commit()
        await session.refresh(p)
        return p.id, admin.id


def hdrs(uid: uuid.UUID) -> dict:
    return {"X-User-Id": str(uid)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_types(client, project_and_admin):
    pid, uid = project_and_admin
    r = await client.get("/connectors/types", headers=hdrs(uid))
    assert r.status_code == 200, r.text
    types = {t["type"] for t in r.json()}
    assert {"csv", "json", "postgresql"}.issubset(types)


@pytest.mark.asyncio
async def test_create_and_list_and_get(client, project_and_admin):
    pid, uid = project_and_admin
    body = {
        "type": "csv",
        "name": "My CSV",
        "description": "desc",
        "config": {"path": "/tmp/x.csv"},
        "secret_fields": [],
    }
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    assert r.status_code == 201, r.text
    data = r.json()
    cid = data["id"]
    assert data["config"]["path"] == "/tmp/x.csv"

    r = await client.get(f"/connectors?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 200
    assert any(c["id"] == str(cid) for c in r.json())

    r = await client.get(f"/connectors/{cid}?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 200
    assert r.json()["name"] == "My CSV"


@pytest.mark.asyncio
async def test_secret_masked_by_default(client, project_and_admin):
    pid, uid = project_and_admin
    body = {
        "type": "postgresql",
        "name": "pg",
        "config": {"password": "secret123", "host": "x.com"},
        "secret_fields": ["password"],
    }
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    assert r.status_code == 201
    cid = r.json()["id"]

    r = await client.get(f"/connectors/{cid}?project_id={pid}", headers=hdrs(uid))
    d = r.json()
    assert d["config"]["password"] == "***"
    assert d["is_secrets_revealed"] is False

    r = await client.get(f"/connectors/{cid}?project_id={pid}&reveal=true", headers=hdrs(uid))
    d = r.json()
    assert d["config"]["password"] == "secret123"
    assert d["is_secrets_revealed"] is True


@pytest.mark.asyncio
async def test_db_stores_encrypted(client, project_and_admin):
    pid, uid = project_and_admin
    body = {
        "type": "postgresql",
        "name": "pg2",
        "config": {"password": "db_secret"},
        "secret_fields": ["password"],
    }
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    cid = r.json()["id"]

    async with (await _get_async_factory())() as s:
        from src.db.connector import Connector
        res = await s.execute(select(Connector).where(Connector.id == uuid.UUID(cid)))
        c = res.scalar_one()
        assert c.config["password"].startswith("enc:v1:")
        assert c.config["password"] != "db_secret"


@pytest.mark.asyncio
async def test_unknown_type(client, project_and_admin):
    pid, uid = project_and_admin
    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "mysql", "name": "x", "config": {}},
        headers=hdrs(uid),
    )
    assert r.status_code == 400
    assert "not registered" in r.text.lower()


@pytest.mark.asyncio
async def test_patch_rename(client, project_and_admin):
    pid, uid = project_and_admin
    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "csv", "name": "old", "config": {"path": "/x.csv"}},
        headers=hdrs(uid),
    )
    cid = r.json()["id"]
    r = await client.patch(
        f"/connectors/{cid}?project_id={pid}",
        json={"name": "renamed"},
        headers=hdrs(uid),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"


@pytest.mark.asyncio
async def test_soft_delete(client, project_and_admin):
    pid, uid = project_and_admin
    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "csv", "name": "del", "config": {"path": "/x.csv"}},
        headers=hdrs(uid),
    )
    cid = r.json()["id"]
    r = await client.delete(f"/connectors/{cid}?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 204
    r = await client.get(f"/connectors?project_id={pid}", headers=hdrs(uid))
    assert all(c["id"] != cid for c in r.json())


@pytest.mark.asyncio
async def test_test_connection_ok(client, project_and_admin, tmp_path):
    pid, uid = project_and_admin
    p = tmp_path / "ok.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=["a"]).writeheader()
    body = {"type": "csv", "name": "ok", "config": {"path": str(p)}}
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    cid = r.json()["id"]
    r = await client.post(f"/connectors/{cid}/test?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_test_connection_fail(client, project_and_admin):
    pid, uid = project_and_admin
    body = {"type": "csv", "name": "bad", "config": {"path": "/nope.csv"}}
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    cid = r.json()["id"]
    r = await client.post(f"/connectors/{cid}/test?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 200
    assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_tables_and_snapshot(client, project_and_admin, tmp_path):
    pid, uid = project_and_admin
    p = tmp_path / "snap.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name"])
        w.writeheader()
        for i in range(10):
            w.writerow({"id": str(i), "name": f"n{i}"})
    body = {"type": "csv", "name": "x", "config": {"path": str(p)}}
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    cid = r.json()["id"]

    r = await client.get(f"/connectors/{cid}/tables?project_id={pid}", headers=hdrs(uid))
    assert r.status_code == 200
    tables = r.json()
    assert len(tables) == 1
    table_name = tables[0]["name"]

    r = await client.post(
        f"/connectors/{cid}/snapshot?project_id={pid}",
        json={"table": table_name, "limit": 3, "offset": 2},
        headers=hdrs(uid),
    )
    assert r.status_code == 200
    snap = r.json()
    assert snap["row_count"] == 3
    assert snap["rows"][0]["name"] == "n2"
    assert snap["truncated"] is True  # 10 > 3


async def _get_async_factory():
    import importlib
    conn_mod = importlib.import_module("src.db.connection")
    return conn_mod.async_session_factory


# ---------------------------------------------------------------------------
# HIA-67 B2: Connector Snapshot → Evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_to_evidence_creates_source_and_evidence(client, project_and_admin, tmp_path):
    """HIA-67 验收: snapshot-to-evidence 创建 Source + Evidence 记录。"""
    pid, uid = project_and_admin
    # 创建 CSV connector（可用作快照测试）
    p = tmp_path / "ev.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "name", "age"])
        w.writeheader()
        for i in range(10):
            w.writerow({"email": f"u{i}@x.com", "name": f"User{i}", "age": str(20 + i)})

    body = {
        "type": "csv",
        "name": "users-ev",
        "config": {"path": str(p)},
        "secret_fields": [],
    }
    r = await client.post(f"/connectors?project_id={pid}", json=body, headers=hdrs(uid))
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # 拿表名
    r = await client.get(f"/connectors/{cid}/tables?project_id={pid}", headers=hdrs(uid))
    table_name = r.json()[0]["name"]

    # 快照 → evidence
    r = await client.post(
        f"/connectors/{cid}/snapshot-to-evidence?project_id={pid}",
        json={"table": table_name, "limit": 5, "auto_evidence": True},
        headers=hdrs(uid),
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert "source_id" in data
    assert "snapshot_id" in data
    assert data["table"] == table_name
    assert data["row_count"] == 5
    assert len(data["evidence_ids"]) == 3  # email, name, age 三个字段

    # profile 包含 null_ratio / unique_ratio / inferred_type
    profile = data["profile"]
    assert "email" in profile
    assert profile["email"]["inferred_type"] == "string"
    assert "sample_values" in profile["email"]
    assert 0.0 <= profile["email"]["null_ratio"] <= 1.0


@pytest.mark.asyncio
async def test_snapshot_to_evidence_auto_evidence_false(client, project_and_admin, tmp_path):
    """auto_evidence=false 时只创建 Source，不创建 Evidence。"""
    pid, uid = project_and_admin
    p = tmp_path / "no-ev.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["x", "y"])
        w.writeheader()
        for i in range(5):
            w.writerow({"x": f"x{i}", "y": str(i)})

    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "csv", "name": "no-ev", "config": {"path": str(p)}},
        headers=hdrs(uid),
    )
    cid = r.json()["id"]

    r = await client.get(f"/connectors/{cid}/tables?project_id={pid}", headers=hdrs(uid))
    table_name = r.json()[0]["name"]

    r = await client.post(
        f"/connectors/{cid}/snapshot-to-evidence?project_id={pid}",
        json={"table": table_name, "auto_evidence": False},
        headers=hdrs(uid),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["source_id"]
    assert data["snapshot_id"]
    assert len(data["evidence_ids"]) == 0  # 没创建 Evidence
    assert len(data["profile"]) == 2  # 但字段统计还是有的


@pytest.mark.asyncio
async def test_snapshot_to_evidence_cross_project(client, project_and_admin, isolated_app, tmp_path):
    """snapshot-to-evidence 跨项目 → 404（隔离验证）。"""
    pid, uid = project_and_admin
    p = tmp_path / "cross.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["a"])
        w.writeheader()
        w.writerow({"a": "1"})

    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "csv", "name": "cross", "config": {"path": str(p)}},
        headers=hdrs(uid),
    )
    cid = r.json()["id"]

    r = await client.get(f"/connectors/{cid}/tables?project_id={pid}", headers=hdrs(uid))
    table_name = r.json()[0]["name"]

    # 用另一个项目 ID（不存在的）
    other_pid = uuid.uuid4()
    r = await client.post(
        f"/connectors/{cid}/snapshot-to-evidence?project_id={other_pid}",
        json={"table": table_name},
        headers=hdrs(uid),
    )
    assert r.status_code == 404  # 跨项目隔离 → 404


@pytest.mark.asyncio
async def test_snapshot_to_evidence_field_type_inference(client, project_and_admin, tmp_path):
    """PG 列类型 → 本体数据类型的映射正确。"""
    pid, uid = project_and_admin
    p = tmp_path / "types.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["uid", "is_active", "score", "joined_at"])
        w.writeheader()
        w.writerow({"uid": "123", "is_active": "true", "score": "99.5", "joined_at": "2024-01-01"})

    r = await client.post(
        f"/connectors?project_id={pid}",
        json={"type": "csv", "name": "types", "config": {"path": str(p)}},
        headers=hdrs(uid),
    )
    cid = r.json()["id"]

    r = await client.get(f"/connectors/{cid}/tables?project_id={pid}", headers=hdrs(uid))
    table_name = r.json()[0]["name"]

    r = await client.post(
        f"/connectors/{cid}/snapshot-to-evidence?project_id={pid}",
        json={"table": table_name, "auto_evidence": True},
        headers=hdrs(uid),
    )
    assert r.status_code == 201, r.text
    profile = r.json()["profile"]

    # CSV 不做类型推断 → 都是 string（PG connector 会用真实 PG 类型）
    assert profile["uid"]["inferred_type"] == "string"
    assert profile["is_active"]["inferred_type"] == "string"
    assert profile["score"]["inferred_type"] == "string"
    assert profile["joined_at"]["inferred_type"] == "string"

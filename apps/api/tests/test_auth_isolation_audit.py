"""HIA-51 集成测试 — auth / 角色 / 跨项目隔离 / 审计。

每个测试用独立的 SQLite (file::memory:?uri=true) 跑一遍 alembic up + 测试。
通过 ASGI 直接打 app，不走 socket。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# 让 ``from src....`` 工作
import sys

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

from src.db.connection import (  # noqa: E402
    async_session_factory,
    init_db,
)
from src.db.identity import Membership, Role  # noqa: E402


# ---------- per-test 数据库覆盖 ----------

@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    """每个测试函数拿到独立的 SQLite 文件 + 已升级到 head 的 schema。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # 清掉 lru_cache 让 settings 重新读 env
    from src.core import config as cfg

    cfg.get_settings.cache_clear()

    # 重建 async/sync engine，让它们使用新的 DATABASE_URL
    from src.db import connection as conn

    await conn.reinit_engines()
    async_session_factory = conn.async_session_factory

    # 同步 DB 跑 alembic up head
    sync_url = f"sqlite:///{db_path}"
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    # 强制 alembic 使用 project root 作为脚本目录（env.py 期望此 cwd）
    cwd_save = os.getcwd()
    try:
        os.chdir(ROOT / "apps" / "api")
        command.upgrade(alembic_cfg, "head")
    finally:
        os.chdir(cwd_save)

    # 现在 import app（拿到的是 patch 后的 settings）
    from src.api.main import app

    # bootstrap admin
    from src.api.auth import ensure_bootstrap_admin

    async with async_session_factory() as s:
        await ensure_bootstrap_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client


@pytest_asyncio.fixture
async def client(isolated_app):
    _, c = isolated_app
    return c


# ---------- helpers ----------

EMAIL_ADMIN = "admin@ontolohub.local"
EMAIL_ALICE = "alice@example.com"
EMAIL_BOB = "bob@example.com"
EMAIL_CAROL = "carol@example.com"


async def _create_user(client: AsyncClient, email: str, name: str | None = None) -> dict:
    """通过 header 触发 lazy create；返回 user 字典。"""
    resp = await client.get("/api/users/me", headers={"X-User-Email": email, "X-User-Name": name or email.split("@")[0]})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- Tests ----------

@pytest.mark.asyncio
async def test_bootstrap_admin_and_me(client: AsyncClient):
    # 1) 不带 header 应当 fallback 到 bootstrap admin
    resp = await client.get("/api/users/me")
    assert resp.status_code == 200
    me = resp.json()
    assert me["user"]["email"] == EMAIL_ADMIN
    assert me["is_admin"] is True
    assert me["memberships"] == []


@pytest.mark.asyncio
async def test_lazy_user_create_via_header(client: AsyncClient):
    resp = await client.get("/api/users/me", headers={"X-User-Email": EMAIL_ALICE})
    assert resp.status_code == 200
    me = resp.json()
    assert me["user"]["email"] == EMAIL_ALICE
    assert me["is_admin"] is False

    # 持久化检查
    me2 = (await client.get("/api/users/me", headers={"X-User-Email": EMAIL_ALICE})).json()
    assert me2["user"]["id"] == me["user"]["id"]


@pytest.mark.asyncio
async def test_create_project_makes_creator_owner(client: AsyncClient):
    headers = {"X-User-Email": EMAIL_ALICE}
    resp = await client.post("/projects", json={"name": "Acme Ontology"}, headers=headers)
    assert resp.status_code == 201, resp.text
    proj = resp.json()
    assert proj["name"] == "Acme Ontology"
    assert proj["my_role"] == "owner"

    # alice 能在列表里看到这个项目
    listing = (await client.get("/projects", headers=headers)).json()
    assert any(p["id"] == proj["id"] for p in listing)


@pytest.mark.asyncio
async def test_cross_project_isolation_returns_404(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}
    b_h = {"X-User-Email": EMAIL_BOB}

    proj_alice = (await client.post("/projects", json={"name": "Alice P"}, headers=a_h)).json()
    proj_bob = (await client.post("/projects", json={"name": "Bob P"}, headers=b_h)).json()

    # Bob 访问 Alice 的项目 → 404（不泄漏存在性）
    r = await client.get(f"/projects/{proj_alice['id']}", headers=b_h)
    assert r.status_code == 404
    # Bob 列表里没有 Alice 的项目
    listing = (await client.get("/projects", headers=b_h)).json()
    assert all(p["id"] != proj_alice["id"] for p in listing)
    assert any(p["id"] == proj_bob["id"] for p in listing)


@pytest.mark.asyncio
async def test_viewer_cannot_create_use_case(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}
    b_h = {"X-User-Email": EMAIL_BOB}

    proj = (await client.post("/projects", json={"name": "P"}, headers=a_h)).json()

    # 把 Bob 设为 VIEWER
    r = await client.post(
        f"/projects/{proj['id']}/members",
        json={"email": EMAIL_BOB, "role": "viewer"},
        headers=a_h,
    )
    assert r.status_code == 201, r.text

    # Bob VIEWER 只能 GET，不能 POST
    listing = await client.get(f"/projects/{proj['id']}/use-cases", headers=b_h)
    assert listing.status_code == 200

    create = await client.post(
        f"/projects/{proj['id']}/use-cases",
        json={"name": "UC1"},
        headers=b_h,
    )
    assert create.status_code == 404  # 视图隔离语义：不足权限时按 404 处理


@pytest.mark.asyncio
async def test_editor_can_create_use_case_and_audit_is_recorded(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}
    b_h = {"X-User-Email": EMAIL_BOB}

    proj = (await client.post("/projects", json={"name": "P"}, headers=a_h)).json()

    r = await client.post(
        f"/projects/{proj['id']}/members",
        json={"email": EMAIL_BOB, "role": "editor"},
        headers=a_h,
    )
    assert r.status_code == 201, r.text

    create = await client.post(
        f"/projects/{proj['id']}/use-cases",
        json={"name": "Batch traceability", "business_problem": "which batch?"},
        headers=b_h,
    )
    assert create.status_code == 201, create.text
    uc = create.json()
    assert uc["name"] == "Batch traceability"

    # 审计：创建 use_case + 创建 project + 创建 membership 至少有 3 条
    audit = await client.get(
        f"/projects/{proj['id']}/audit", headers=a_h
    )
    assert audit.status_code == 200
    events = audit.json()
    types = [e["event_type"] for e in events]
    assert "create" in types
    # 至少有 3 条 create
    creates = [e for e in events if e["event_type"] == "create"]
    assert len(creates) >= 3
    # entry_hash 链路存在
    for e in events:
        assert e["entry_hash"]
        assert len(e["entry_hash"]) == 64


@pytest.mark.asyncio
async def test_audit_hash_chain_is_intact(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}

    proj = (await client.post("/projects", json={"name": "P"}, headers=a_h)).json()
    await client.post(
        f"/projects/{proj['id']}/use-cases",
        json={"name": "UC1"},
        headers=a_h,
    )
    await client.post(
        f"/projects/{proj['id']}/requirements",
        json={"name": "R1"},
        headers=a_h,
    )

    # admin 调用 verify
    verify = await client.get("/audit/verify", params={"project_id": proj["id"]})
    assert verify.status_code == 200
    body = verify.json()
    assert body["ok"] is True
    assert body["checked"] >= 3


@pytest.mark.asyncio
async def test_admin_sees_all_projects(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}
    b_h = {"X-User-Email": EMAIL_BOB}
    admin_h = {"X-User-Email": EMAIL_ADMIN}

    p_alice = (await client.post("/projects", json={"name": "A"}, headers=a_h)).json()
    p_bob = (await client.post("/projects", json={"name": "B"}, headers=b_h)).json()

    admin_list = (await client.get("/projects", headers=admin_h)).json()
    ids = {p["id"] for p in admin_list}
    assert p_alice["id"] in ids
    assert p_bob["id"] in ids


@pytest.mark.asyncio
async def test_owner_only_can_invite_and_remove(client: AsyncClient):
    a_h = {"X-User-Email": EMAIL_ALICE}
    b_h = {"X-User-Email": EMAIL_BOB}

    proj = (await client.post("/projects", json={"name": "P"}, headers=a_h)).json()
    # 把 Bob 加为 EDITOR
    r = await client.post(
        f"/projects/{proj['id']}/members",
        json={"email": EMAIL_BOB, "role": "editor"},
        headers=a_h,
    )
    assert r.status_code == 201

    bob_id = (await client.get("/api/users/me", headers=b_h)).json()["user"]["id"]

    # Bob 是 EDITOR，邀请应被拒（要求 OWNER）→ 404
    r2 = await client.post(
        f"/projects/{proj['id']}/members",
        json={"email": "carol@example.com", "role": "viewer"},
        headers=b_h,
    )
    assert r2.status_code == 404

    # OWNER (alice) 移除 Bob
    rm = await client.delete(
        f"/projects/{proj['id']}/members/{bob_id}", headers=a_h
    )
    assert rm.status_code == 204

    # Bob 之后访问项目也应是 404
    g = await client.get(f"/projects/{proj['id']}", headers=b_h)
    assert g.status_code == 404

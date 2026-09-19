"""HIA-77 D1 — Workspace / multi-tenant tests.

Coverage:

* Workspace CRUD + slug validation
* Lazy auto-create of a default workspace on first access
* Membership: invite / role change / remove + self-leave
* Owner protection (only owner can delete / transfer)
* Quota snapshot
* Cross-workspace isolation (Bob in WS-A cannot see WS-B)
* Project → workspace_id plumbing (projects auto-bound at create)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))


# ---------- per-test DB override ----------


@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client


@pytest_asyncio.fixture
async def client(isolated_app):
    _, c = isolated_app
    return c


# ---------- helpers ----------


ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"


async def _create_user(client: AsyncClient, email: str) -> str:
    """Provision a User row directly via DB (no public endpoint for this)."""
    from src.db.connection import async_session_factory
    from src.db.identity import User

    async with async_session_factory() as s:
        u = User(email=email, display_name=email.split("@")[0], global_role="user", is_active=True)
        s.add(u)
        await s.commit()
        return str(u.id)


# ===========================================================================
# 1. Default workspace auto-creation
# ===========================================================================


@pytest.mark.asyncio
async def test_list_workspaces_auto_creates_default(client: AsyncClient):
    """First GET /api/workspaces for a user with no workspace creates a default."""
    r = await client.get("/api/workspaces", headers={"X-User-Email": ALICE})
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert items[0]["is_owner"] is True
    assert items[0]["role"] == "owner"


@pytest.mark.asyncio
async def test_list_workspaces_idempotent(client: AsyncClient):
    r1 = (await client.get(
        "/api/workspaces", headers={"X-User-Email": ALICE}
    )).json()
    r2 = (await client.get(
        "/api/workspaces", headers={"X-User-Email": ALICE}
    )).json()
    assert len(r1) == 1
    assert len(r2) == 1
    assert r1[0]["id"] == r2[0]["id"]


@pytest.mark.asyncio
async def test_my_workspaces_alias(client: AsyncClient):
    """``/api/users/me/workspaces`` returns the same data."""
    r1 = (await client.get(
        "/api/workspaces", headers={"X-User-Email": ALICE}
    )).json()
    r2 = (await client.get(
        "/api/users/me/workspaces", headers={"X-User-Email": ALICE}
    )).json()
    assert r1[0]["id"] == r2[0]["id"]


# ===========================================================================
# 2. Create workspace
# ===========================================================================


@pytest.mark.asyncio
async def test_create_workspace(client: AsyncClient):
    # Need a known user first
    await _create_user(client, ALICE)
    r = await client.post(
        "/api/workspaces",
        json={"name": "Acme Inc", "slug": "acme"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "acme"
    assert body["plan"] == "free"

    # Creator is auto-joined as OWNER
    members = (await client.get(
        f"/api/workspaces/{body['id']}/members",
        headers={"X-User-Email": ALICE},
    )).json()
    assert len(members) == 1
    assert members[0]["role"] == "owner"


@pytest.mark.asyncio
async def test_create_workspace_invalid_slug_422(client: AsyncClient):
    await _create_user(client, ALICE)
    r = await client.post(
        "/api/workspaces",
        json={"name": "Bad", "slug": "Bad Slug!"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_workspace_duplicate_slug_409(client: AsyncClient):
    await _create_user(client, ALICE)
    await client.post(
        "/api/workspaces",
        json={"name": "A", "slug": "dup"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.post(
        "/api/workspaces",
        json={"name": "B", "slug": "dup"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_create_workspace_with_plan(client: AsyncClient):
    await _create_user(client, ALICE)
    r = await client.post(
        "/api/workspaces",
        json={"name": "Big Co", "slug": "bigco", "plan": "pro"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    assert r.json()["plan"] == "pro"


# ===========================================================================
# 3. Detail / Update / Delete
# ===========================================================================


@pytest.mark.asyncio
async def test_get_workspace_requires_membership(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "Private", "slug": "priv"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Bob has no membership → 404 (not 403, to avoid leaking existence)
    r = await client.get(
        f"/api/workspaces/{wid}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_workspace_admin_required(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "Acme", "slug": "acme-x"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Invite Bob as VIEWER (admin-only operation)
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )

    # Bob is viewer → cannot update workspace settings
    r = await client.patch(
        f"/api/workspaces/{wid}",
        json={"description": "hijacked"},
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 403, r.text

    # Alice can
    r = await client.patch(
        f"/api/workspaces/{wid}",
        json={"description": "updated"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["description"] == "updated"


@pytest.mark.asyncio
async def test_update_workspace_settings_merge(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    r = await client.patch(
        f"/api/workspaces/{wid}",
        json={"settings": {"allow_public_catalog": True}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["settings"]["allow_public_catalog"] is True

    # Re-patch another key — first key should be preserved
    r = await client.patch(
        f"/api/workspaces/{wid}",
        json={"settings": {"max_projects": 99}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["max_projects"] == 99
    assert body["settings"]["allow_public_catalog"] is True


@pytest.mark.asyncio
async def test_delete_workspace_owner_only(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Invite Bob as admin
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "admin"},
        headers={"X-User-Email": ALICE},
    )

    # Bob (admin) cannot delete
    r = await client.delete(
        f"/api/workspaces/{wid}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 403, r.text

    # Alice (owner) can
    r = await client.delete(
        f"/api/workspaces/{wid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 204

    # Subsequent GET → 404
    r = await client.get(
        f"/api/workspaces/{wid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


# ===========================================================================
# 4. Membership
# ===========================================================================


@pytest.mark.asyncio
async def test_invite_member_admin_required(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    await _create_user(client, CAROL)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Invite Bob as admin
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "admin"},
        headers={"X-User-Email": ALICE},
    )

    # Bob (admin) can invite Carol
    r = await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": CAROL, "role": "viewer"},
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 201, r.text
    members = (await client.get(
        f"/api/workspaces/{wid}/members",
        headers={"X-User-Email": ALICE},
    )).json()
    assert len(members) == 3


@pytest.mark.asyncio
async def test_invite_member_viewer_forbidden(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    await _create_user(client, CAROL)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )

    r = await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": CAROL, "role": "viewer"},
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_invite_already_member_409(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "admin"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_change_role_owner_only(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )

    # Promote Bob to admin by upgrading his role to admin first
    r = await client.patch(
        f"/api/workspaces/{wid}/members/{await _lookup_user_id(client, BOB)}",
        json={"role": "admin"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "admin"

    # Now Bob (admin) tries to change Carol's role — must fail
    await _create_user(client, CAROL)
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": CAROL, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.patch(
        f"/api/workspaces/{wid}/members/{await _lookup_user_id(client, CAROL)}",
        json={"role": "admin"},
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_remove_member_self_leave(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )

    # Bob self-leaves
    r = await client.delete(
        f"/api/workspaces/{wid}/members/{await _lookup_user_id(client, BOB)}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 204

    # Bob can no longer see the workspace
    r = await client.get(
        f"/api/workspaces/{wid}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_remove_owner(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Bob is admin — Alice (owner) cannot be removed by admin
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "admin"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.delete(
        f"/api/workspaces/{wid}/members/{await _lookup_user_id(client, ALICE)}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 409


async def _lookup_user_id(client: AsyncClient, email: str) -> str:
    """Async DB lookup to get a user id (test-only helper)."""
    from src.db.connection import async_session_factory
    from src.db.identity import User
    from sqlalchemy import select

    async with async_session_factory() as s:
        u = (await s.execute(
            select(User).where(User.email == email)
        )).scalar_one()
        return str(u.id)


# ===========================================================================
# 5. Cross-workspace isolation
# ===========================================================================


@pytest.mark.asyncio
async def test_workspace_isolation(client: AsyncClient):
    """Alice's workspace is invisible to Bob."""
    await _create_user(client, ALICE)
    await _create_user(client, BOB)

    # Create Acme (Alice)
    alice_ws = (await client.post(
        "/api/workspaces",
        json={"name": "Acme", "slug": "acme-i"},
        headers={"X-User-Email": ALICE},
    )).json()

    # Create Globex (Bob)
    bob_ws = (await client.post(
        "/api/workspaces",
        json={"name": "Globex", "slug": "globex-i"},
        headers={"X-User-Email": BOB},
    )).json()

    # Alice only sees Acme
    alice_list = (await client.get(
        "/api/workspaces", headers={"X-User-Email": ALICE}
    )).json()
    assert len(alice_list) == 1
    assert alice_list[0]["slug"] == "acme-i"

    # Bob only sees Globex
    bob_list = (await client.get(
        "/api/workspaces", headers={"X-User-Email": BOB}
    )).json()
    assert len(bob_list) == 1
    assert bob_list[0]["slug"] == "globex-i"

    # Alice cannot GET Bob's workspace
    r = await client.get(
        f"/api/workspaces/{bob_ws['id']}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404

    # Bob cannot GET Alice's workspace
    r = await client.get(
        f"/api/workspaces/{alice_ws['id']}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invite_then_visible_to_invitee(client: AsyncClient):
    """After Alice invites Bob, Bob sees Acme in his workspace list."""
    await _create_user(client, ALICE)
    await _create_user(client, BOB)

    wid = (await client.post(
        "/api/workspaces",
        json={"name": "Acme", "slug": "acme-inv"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Invite Bob
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "member"},
        headers={"X-User-Email": ALICE},
    )

    # Bob now sees the workspace (auto_create=True is fine; he's already a member)
    bob_list = (await client.get(
        "/api/workspaces", headers={"X-User-Email": BOB}
    )).json()
    assert len(bob_list) == 1
    assert bob_list[0]["slug"] == "acme-inv"
    assert bob_list[0]["role"] == "member"
    assert bob_list[0]["is_owner"] is False


# ===========================================================================
# 6. Quota snapshot
# ===========================================================================


@pytest.mark.asyncio
async def test_quota_default_free_plan(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    r = await client.get(
        f"/api/workspaces/{wid}/quota",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] == "free"
    # Default free quotas
    assert body["max_projects"] == 3
    assert body["max_users"] == 5
    assert body["max_objects"] == 10000
    # 1 user (Alice) — owner counts
    assert body["current_users"] == 1
    assert body["current_projects"] == 0


@pytest.mark.asyncio
async def test_quota_settings_override(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.patch(
        f"/api/workspaces/{wid}",
        json={"settings": {"max_projects": 100}},
        headers={"X-User-Email": ALICE},
    )
    r = await client.get(
        f"/api/workspaces/{wid}/quota",
        headers={"X-User-Email": ALICE},
    )
    body = r.json()
    assert body["max_projects"] == 100
    # max_users / max_objects still default
    assert body["max_users"] == 5


@pytest.mark.asyncio
async def test_quota_counts_invited_users(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    await _create_user(client, CAROL)
    wid = (await client.post(
        "/api/workspaces",
        json={"name": "X", "slug": "xx"},
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": BOB, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )
    await client.post(
        f"/api/workspaces/{wid}/members",
        json={"email": CAROL, "role": "viewer"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.get(
        f"/api/workspaces/{wid}/quota",
        headers={"X-User-Email": ALICE},
    )
    assert r.json()["current_users"] == 3

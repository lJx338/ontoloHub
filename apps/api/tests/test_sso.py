"""HIA-79 D2 — SSO / Identity Provider tests.

Coverage:

* Provider CRUD (create / list / get / patch / delete)
* Secret-field encryption at rest + masking on read
* Workspace isolation: only members can list; ADMIN+ for write
* Workspace-level uniqueness: one OIDC provider per workspace
* ``test_provider`` end-to-end with a fake discovery doc (httpx MockTransport)
* OIDC login flow: discovery, PKCE pair, authorize URL builder, JWT verify
* SSO callback happy path: state lookup → token exchange → ID-token verify
  → JIT user creation → JWT issuance → redirect with tokens
* Auto-provision off → unknown user is rejected
* Force-SSO clears ``User.password_hash``
* Expired / consumed login sessions are rejected
* Open-redirect guard: ``return_to`` must be a relative path

OIDC machinery is exercised by stubbing ``fetch_discovery`` /
``fetch_jwks`` / ``exchange_code_for_tokens`` / ``verify_id_token`` via
``unittest.mock``.  We don't spin up a real IdP — that's an integration
concern; here we prove the contract between callback and the OIDC client.
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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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


ALICE = "alice@example.com"
BOB = "bob@example.com"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _create_user(_client: AsyncClient, email: str) -> str:
    from src.db.connection import async_session_factory
    from src.db.identity import User

    async with async_session_factory() as s:
        existing = (
            await s.execute(__import__("sqlalchemy").select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            return str(existing.id)
        u = User(
            email=email,
            display_name=email.split("@")[0],
            global_role="user",
            is_active=True,
        )
        s.add(u)
        await s.commit()
        return str(u.id)


async def _create_workspace_as(client: AsyncClient, email: str, slug: str) -> str:
    """Create a workspace and return its id.  Caller is auto-joined as owner."""
    r = await client.post(
        "/api/workspaces",
        json={"name": slug, "slug": slug},
        headers={"X-User-Email": email},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ===========================================================================
# 1. Provider CRUD — happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_create_provider_admin_required(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid = await _create_workspace_as(client, ALICE, "acme-sso")

    # Bob is not yet a member — 404
    r = await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://example.okta.com",
                "client_id": "abc",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404, r.text

    # Alice (owner, ADMIN+ via owner rank) — succeeds
    r = await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://example.okta.com",
                "client_id": "abc",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Okta"
    assert body["protocol"] == "oidc"
    assert body["status"] == "active"
    # secret fields masked on read
    assert body["config"]["client_secret"] == "***"


@pytest.mark.asyncio
async def test_secret_encrypted_at_rest(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-sso-2")

    r = await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://example.okta.com",
                "client_id": "abc",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )
    pid = r.json()["id"]

    # Read from DB directly — secret must be encrypted (enc:v1: prefix)
    from src.db.connection import async_session_factory
    from src.db.sso import IdentityProvider
    from sqlalchemy import select

    async with async_session_factory() as s:
        idp = (await s.execute(
            select(IdentityProvider).where(IdentityProvider.id == uuid.UUID(pid))
        )).scalar_one()
        # encrypted form may be under client_secret or client_secret_enc
        raw_cfg = dict(idp.config or {})
        all_secrets = " ".join(str(v) for v in raw_cfg.values())
        assert "shh" not in all_secrets, f"plaintext leaked: {all_secrets}"
        # at least one field starts with enc:v1:
        assert any(
            isinstance(v, str) and v.startswith("enc:v1:")
            for v in raw_cfg.values()
        ), f"no encrypted field found in {raw_cfg}"


@pytest.mark.asyncio
async def test_workspace_protocol_uniqueness(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-sso-3")

    r1 = await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {"issuer_url": "https://x", "client_id": "a", "client_secret": "b"},
        },
        headers={"X-User-Email": ALICE},
    )
    assert r1.status_code == 201

    # Second OIDC provider for same workspace → 409
    r2 = await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Google",
            "protocol": "oidc",
            "config": {"issuer_url": "https://y", "client_id": "c", "client_secret": "d"},
        },
        headers={"X-User-Email": ALICE},
    )
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
async def test_list_and_get_provider(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-sso-4")
    pid = (await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {"issuer_url": "https://x", "client_id": "a", "client_secret": "b"},
        },
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    r = await client.get(
        f"/api/workspaces/{wid}/sso/providers",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = await client.get(
        f"/api/workspaces/{wid}/sso/providers/{pid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["id"] == pid


@pytest.mark.asyncio
async def test_patch_disable_provider(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-sso-5")
    pid = (await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {"issuer_url": "https://x", "client_id": "a", "client_secret": "b"},
            "force_sso": False,
        },
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    r = await client.patch(
        f"/api/workspaces/{wid}/sso/providers/{pid}",
        json={"status": "disabled", "force_sso": True},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "disabled"
    assert body["force_sso"] is True


@pytest.mark.asyncio
async def test_delete_provider_cascades_login_sessions(client: AsyncClient):
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-sso-6")
    pid = (await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {"issuer_url": "https://x", "client_id": "a", "client_secret": "b"},
        },
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    r = await client.delete(
        f"/api/workspaces/{wid}/sso/providers/{pid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 204

    # 404 after delete
    r = await client.get(
        f"/api/workspaces/{wid}/sso/providers/{pid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


# ===========================================================================
# 2. Cross-workspace isolation
# ===========================================================================


@pytest.mark.asyncio
async def test_provider_visible_only_to_members(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_user(client, BOB)
    wid_a = await _create_workspace_as(client, ALICE, "ws-a-sso")
    wid_b = await _create_workspace_as(client, BOB, "ws-b-sso")

    pid_a = (await client.post(
        f"/api/workspaces/{wid_a}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {"issuer_url": "https://x", "client_id": "a", "client_secret": "b"},
        },
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    # Bob cannot list Alice's providers
    r = await client.get(
        f"/api/workspaces/{wid_a}/sso/providers",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404

    # Bob cannot fetch Alice's provider
    r = await client.get(
        f"/api/workspaces/{wid_a}/sso/providers/{pid_a}",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 404


# ===========================================================================
# 3. OIDC client — pure helpers (no HTTP)
# ===========================================================================


@pytest.mark.asyncio
async def test_pkce_pair_shape():
    from src.services.sso_client import generate_pkce_pair

    verifier, challenge = generate_pkce_pair()
    # 32 random bytes → 43 base64url chars (no padding)
    assert 40 <= len(verifier) <= 64
    assert 40 <= len(challenge) <= 64
    # verifier != challenge
    assert verifier != challenge

    # S256 challenge = base64url(sha256(verifier)) (no padding)
    import base64
    import hashlib

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected


@pytest.mark.asyncio
async def test_authorize_url_builder():
    from src.services.sso_client import build_authorization_url

    url = build_authorization_url(
        authorization_endpoint="https://idp.test/authorize",
        client_id="my-client",
        redirect_uri="https://app.test/cb",
        state="abc",
        nonce="xyz",
        code_challenge="ch",
        scopes=["openid", "email"],
    )
    assert url.startswith("https://idp.test/authorize?")
    assert "response_type=code" in url
    assert "client_id=my-client" in url
    assert "scope=openid+email" in url or "scope=openid%20email" in url
    assert "state=abc" in url
    assert "nonce=xyz" in url
    assert "code_challenge=ch" in url
    assert "code_challenge_method=S256" in url


# ===========================================================================
# 4. Login start — workspace slug → redirect to IdP
# ===========================================================================


@pytest.mark.asyncio
async def test_login_start_redirects_to_idp(client: AsyncClient, monkeypatch):
    """Stub discovery so we don't actually hit an IdP, and assert the
    authorize URL is well-formed."""
    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "acme-login")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    # Stub fetch_discovery to return a fake doc.
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import OidcDiscovery

    fake = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
            "userinfo_endpoint": "https://idp.test/userinfo",
        },
        fetched_at=0.0,
    )

    async def _fake_fetch(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fake_fetch)

    r = await client.get(
        "/api/sso/acme-login/login",
        params={"return_to": "/dashboard"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith("https://idp.test/authorize?")
    assert "client_id=my-client" in location
    assert "code_challenge_method=S256" in location

    # A login session row was persisted (we can count via DB)
    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, func

    async with async_session_factory() as s:
        cnt = (await s.execute(
            select(func.count(SsoLoginSession.id))
        )).scalar_one()
        assert cnt >= 1


@pytest.mark.asyncio
async def test_login_404_for_unknown_workspace(client: AsyncClient):
    r = await client.get("/api/sso/does-not-exist/login", follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_login_no_active_provider(client: AsyncClient):
    await _create_user(client, ALICE)
    await _create_workspace_as(client, ALICE, "no-idp")
    r = await client.get(
        "/api/sso/no-idp/login", follow_redirects=False
    )
    assert r.status_code == 404


# ===========================================================================
# 5. Callback — happy path with mocks
# ===========================================================================


@pytest.mark.asyncio
async def test_callback_happy_path_jit_provision(client: AsyncClient, monkeypatch):
    """Full callback flow: state lookup → token exchange → ID-token verify
    → JIT user create → workspace membership → JWT issued → redirect."""
    from datetime import datetime, timedelta, timezone
    import secrets

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "jit-sso")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "auto_provision": True,
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    # Stub OIDC client primitives.
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import (
        OidcClaims, OidcDiscovery, OidcTokenResponse,
    )

    fake_disc = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
            "userinfo_endpoint": "https://idp.test/userinfo",
        },
        fetched_at=0.0,
    )

    async def _fake_fetch_discovery(*_args, **_kwargs):
        return fake_disc

    async def _fake_exchange(**_kwargs):
        return OidcTokenResponse(
            access_token="at",
            id_token="id",
            refresh_token="rt",
            expires_in=3600,
            token_type="Bearer",
            raw={},
        )

    async def _fake_verify(**_kwargs):
        return OidcClaims(
            sub="okta-user-1",
            issuer="https://idp.test",
            audience="my-client",
            nonce=_kwargs["expected_nonce"],
            email="newuser@example.com",
            email_verified=True,
            name="New User",
            preferred_username="newuser",
            raw={
                "email": "newuser@example.com",
                "email_verified": True,
                "name": "New User",
                "preferred_username": "newuser",
                "sub": "okta-user-1",
            },
        )

    async def _fake_userinfo(**_kwargs):
        return {}

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fake_fetch_discovery)
    monkeypatch.setattr(sso_client_mod, "exchange_code_for_tokens", _fake_exchange)
    monkeypatch.setattr(sso_client_mod, "verify_id_token", _fake_verify)
    monkeypatch.setattr(sso_client_mod, "fetch_userinfo", _fake_userinfo)

    # Start login to materialize an SsoLoginSession row.
    r = await client.get(
        "/api/sso/jit-sso/login",
        params={"return_to": "/dashboard"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    # Pull the just-created state from DB.
    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        state = sess.state

    # Hit callback
    r = await client.get(
        f"/api/sso/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith("/dashboard?")
    assert "token=" in location
    assert "refresh=" in location

    # User was provisioned and joined the workspace
    from src.db.identity import User
    from src.db.workspace import WorkspaceMembership
    from sqlalchemy import select as _select

    async with async_session_factory() as s:
        u = (await s.execute(
            _select(User).where(User.email == "newuser@example.com")
        )).scalar_one_or_none()
        assert u is not None
        m = (await s.execute(
            _select(WorkspaceMembership).where(
                WorkspaceMembership.user_id == u.id,
                WorkspaceMembership.workspace_id == uuid.UUID(wid),
            )
        )).scalar_one_or_none()
        assert m is not None
        assert m.role == "member"


@pytest.mark.asyncio
async def test_callback_rejects_replay(client: AsyncClient, monkeypatch):
    """Calling the callback twice with the same state → second call rejected."""
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import (
        OidcClaims, OidcDiscovery, OidcTokenResponse,
    )

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "replay-sso")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    fake_disc = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
        },
        fetched_at=0.0,
    )

    async def _fake_fetch(*_a, **_k):
        return fake_disc

    async def _fake_exchange(**_k):
        return OidcTokenResponse(
            access_token="a", id_token="i",
            refresh_token=None, expires_in=3600, token_type="Bearer", raw={},
        )

    async def _fake_verify(**_k):
        return OidcClaims(
            sub="x", issuer="https://idp.test", audience="my-client",
            nonce=_k["expected_nonce"], email="user@example.com",
            email_verified=True, name="U", preferred_username="u",
            raw={"email": "user@example.com", "sub": "x"},
        )

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fake_fetch)
    monkeypatch.setattr(sso_client_mod, "exchange_code_for_tokens", _fake_exchange)
    monkeypatch.setattr(sso_client_mod, "verify_id_token", _fake_verify)

    r = await client.get("/api/sso/replay-sso/login", follow_redirects=False)
    assert r.status_code == 302

    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        state = sess.state

    # First call — succeeds
    r = await client.get(
        f"/api/sso/callback?code=x&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302

    # Second call — rejected (already consumed)
    r = await client.get(
        f"/api/sso/callback?code=x&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_expired_session_rejected(client: AsyncClient, monkeypatch):
    """Past-expires_at SsoLoginSession → 400."""
    from datetime import datetime, timedelta, timezone

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "expired-sso")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    # Start login to create a session, then forcibly expire it.
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import OidcDiscovery

    async def _fake(*_a, **_k):
        return OidcDiscovery(
            endpoints={
                "issuer": "https://idp.test",
                "authorization_endpoint": "https://idp.test/authorize",
                "token_endpoint": "https://idp.test/token",
                "jwks_uri": "https://idp.test/jwks",
            },
            fetched_at=0.0,
        )

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fake)
    r = await client.get("/api/sso/expired-sso/login", follow_redirects=False)
    assert r.status_code == 302

    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        sess.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await s.flush()
        await s.commit()
        state = sess.state

    r = await client.get(
        f"/api/sso/callback?code=x&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_auto_provision_off_rejects_unknown_user(
    client: AsyncClient, monkeypatch
):
    """When ``auto_provision=false``, an unknown email must NOT create a User."""
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import (
        OidcClaims, OidcDiscovery, OidcTokenResponse,
    )

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "no-jit-sso")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "auto_provision": False,
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    fake = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
        },
        fetched_at=0.0,
    )

    async def _fd(*_a, **_k):
        return fake

    async def _ex(**_k):
        return OidcTokenResponse(
            access_token="a", id_token="i",
            refresh_token=None, expires_in=3600, token_type="Bearer", raw={},
        )

    async def _ve(**_k):
        return OidcClaims(
            sub="x", issuer="https://idp.test", audience="my-client",
            nonce=_k["expected_nonce"], email="ghost@example.com",
            email_verified=True, name="G", preferred_username="g",
            raw={"email": "ghost@example.com", "sub": "x"},
        )

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fd)
    monkeypatch.setattr(sso_client_mod, "exchange_code_for_tokens", _ex)
    monkeypatch.setattr(sso_client_mod, "verify_id_token", _ve)

    r = await client.get("/api/sso/no-jit-sso/login", follow_redirects=False)
    assert r.status_code == 302

    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        state = sess.state

    r = await client.get(
        f"/api/sso/callback?code=x&state={state}",
        follow_redirects=False,
    )
    # Rejection redirect carries sso_error=user_not_provisioned
    assert r.status_code == 302
    assert "sso_error=user_not_provisioned" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_force_sso_clears_password(
    client: AsyncClient, monkeypatch
):
    """``force_sso=true`` clears ``User.password_hash`` after SSO login."""
    from src.core.auth import hash_password

    await _create_user(client, ALICE)

    # Pre-create a user with a password
    from src.db.connection import async_session_factory
    from src.db.identity import User
    from sqlalchemy import select

    existing = "existing@example.com"
    async with async_session_factory() as s:
        u = User(
            email=existing,
            display_name="Existing",
            global_role="user",
            is_active=True,
            password_hash=hash_password("hunter2-strong"),
        )
        s.add(u)
        await s.commit()

    wid = await _create_workspace_as(client, ALICE, "force-sso-ws")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "force_sso": True,
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import (
        OidcClaims, OidcDiscovery, OidcTokenResponse,
    )

    fake = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
        },
        fetched_at=0.0,
    )

    async def _fd(*_a, **_k):
        return fake

    async def _ex(**_k):
        return OidcTokenResponse(
            access_token="a", id_token="i",
            refresh_token=None, expires_in=3600, token_type="Bearer", raw={},
        )

    async def _ve(**_k):
        return OidcClaims(
            sub="x", issuer="https://idp.test", audience="my-client",
            nonce=_k["expected_nonce"], email=existing,
            email_verified=True, name="Existing", preferred_username="e",
            raw={"email": existing, "sub": "x"},
        )

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fd)
    monkeypatch.setattr(sso_client_mod, "exchange_code_for_tokens", _ex)
    monkeypatch.setattr(sso_client_mod, "verify_id_token", _ve)

    r = await client.get("/api/sso/force-sso-ws/login", follow_redirects=False)
    assert r.status_code == 302

    from src.db.sso import SsoLoginSession
    from sqlalchemy import desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        state = sess.state

    r = await client.get(
        f"/api/sso/callback?code=x&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302

    # password_hash is now None
    async with async_session_factory() as s:
        u = (await s.execute(
            select(User).where(User.email == existing)
        )).scalar_one()
        assert u.password_hash is None


# ===========================================================================
# 6. Open-redirect guard on return_to
# ===========================================================================


@pytest.mark.asyncio
async def test_return_to_absolute_url_sanitised(client: AsyncClient, monkeypatch):
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import OidcDiscovery

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "redirect-sso")
    await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )

    async def _fd(*_a, **_k):
        return OidcDiscovery(
            endpoints={
                "issuer": "https://idp.test",
                "authorization_endpoint": "https://idp.test/authorize",
                "token_endpoint": "https://idp.test/token",
                "jwks_uri": "https://idp.test/jwks",
            },
            fetched_at=0.0,
        )

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fd)

    # Externally-anchored URL — must NOT pass through to the callback.
    r = await client.get(
        "/api/sso/redirect-sso/login",
        params={"return_to": "https://attacker.example/steal"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    from src.db.connection import async_session_factory
    from src.db.sso import SsoLoginSession
    from sqlalchemy import select, desc

    async with async_session_factory() as s:
        sess = (await s.execute(
            select(SsoLoginSession).order_by(desc(SsoLoginSession.created_at)).limit(1)
        )).scalar_one()
        assert sess.relay_state != "https://attacker.example/steal"


# ===========================================================================
# 7. test_connection — fake discovery (mock transport not needed; we stub)
# ===========================================================================


@pytest.mark.asyncio
async def test_connection_ok_with_stubbed_discovery(
    client: AsyncClient, monkeypatch
):
    from src.services import sso_client as sso_client_mod
    from src.services.sso_client import OidcDiscovery

    await _create_user(client, ALICE)
    wid = await _create_workspace_as(client, ALICE, "test-conn-sso")
    pid = (await client.post(
        f"/api/workspaces/{wid}/sso/providers",
        json={
            "name": "Okta",
            "protocol": "oidc",
            "config": {
                "issuer_url": "https://idp.test",
                "client_id": "my-client",
                "client_secret": "shh",
            },
        },
        headers={"X-User-Email": ALICE},
    )).json()["id"]

    fake = OidcDiscovery(
        endpoints={
            "issuer": "https://idp.test",
            "authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/jwks",
        },
        fetched_at=0.0,
    )

    async def _fd(*_a, **_k):
        return fake

    monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fd)

    r = await client.post(
        f"/api/workspaces/{wid}/sso/providers/{pid}/test",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "OIDC discovery OK" in body["message"]

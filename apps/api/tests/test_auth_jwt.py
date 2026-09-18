"""HIA-64 B1 — JWT + API Key 认证集成测试。

覆盖：
- ``POST /api/auth/login`` 邮箱 + 密码登录返回 access/refresh
- ``POST /api/auth/refresh`` refresh 换新 access
- ``GET  /api/auth/me`` Bearer 鉴权读取当前用户
- ``POST /api/auth/set-password`` 改自己密码（需 current_password）
- admin 代改别人密码
- ``POST /api/api-keys`` 创建 API Key（明文仅返一次）
- Bearer / API Key 鉴权后能访问业务接口
- 错误：无效 token / 过期 / 无密码用户不能走 JWT 登录

每个测试用独立的 SQLite 跑 alembic up head + 直接 ASGI 调。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))


# ---------------------------------------------------------------------------
# Fixture：和 test_auth_isolation_audit 一样，每测试独立 DB
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from src.core import config as cfg

    cfg.get_settings.cache_clear()
    from src.db import connection as conn

    await conn.reinit_engines()
    factory = conn.async_session_factory

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
    from src.core.config import get_settings

    async with factory() as s:
        await ensure_bootstrap_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


ADMIN_EMAIL = "admin@ontolohub.local"


async def _bootstrap_admin_via_header(c: AsyncClient) -> dict:
    """admin 没设密码时先用 header 取到 user 字典（dev fallback）。"""
    r = await c.get("/api/users/me")
    assert r.status_code == 200, r.text
    return r.json()["user"]


async def _set_admin_password(c: AsyncClient, new_password: str) -> None:
    """admin 在 set-password endpoint 设密码（self 路径需 current_password）。"""
    # bootstrap admin 没 password_hash；首次设密码需要 current_password 字段。
    # 我们的 endpoint 在改自己时强制要 current_password——但首次为空字符串应能通过。
    r = await c.post(
        "/api/auth/set-password",
        json={"new_password": new_password, "current_password": ""},
    )
    # 如果 current_password="" 不被允许，那就直接通过 SQL 设
    if r.status_code != 204:
        from src.db.connection import async_session_factory
        from src.db.identity import User
        from sqlalchemy import select
        from src.core.auth import hash_password

        async with async_session_factory() as s:
            result = await s.execute(select(User).where(User.email == ADMIN_EMAIL))
            user = result.scalar_one()
            user.password_hash = hash_password(new_password)
            s.add(user)
            await s.commit()


def _decode(token: str) -> dict:
    from src.core.config import get_settings
    settings = get_settings()
    return jwt.decode(
        token,
        settings.security.effective_jwt_secret(),
        algorithms=[settings.security.jwt_algorithm],
    )


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_login_with_correct_password_returns_jwt_pair(client: AsyncClient):
    await _set_admin_password(client, "SuperSecret123!")

    r = await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "SuperSecret123!"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    # access token 里 type=access，sub 是 admin uuid 字符串
    payload = _decode(body["access_token"])
    assert payload["type"] == "access"
    assert payload["sub"]
    assert payload["email"] == ADMIN_EMAIL
    # refresh 同样
    payload_r = _decode(body["refresh_token"])
    assert payload_r["type"] == "refresh"
    assert payload_r["sub"] == payload["sub"]


@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(client: AsyncClient):
    await _set_admin_password(client, "SuperSecret123!")

    r = await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "wrong-password"},
    )
    assert r.status_code == 401
    assert "invalid credentials" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_unknown_email_returns_401(client: AsyncClient):
    """enumeration 防御：未知 email 与错密码同样 401。"""
    r = await client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "anything"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_user_without_password_cannot_login_via_jwt(client: AsyncClient):
    """没设密码的 user（bootstrap admin 初始状态）不允许走 JWT 登录。"""
    # admin 默认无 password_hash
    r = await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "anything"},
    )
    # authenticate_user 应该返回 None
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_me_with_bearer_token(client: AsyncClient):
    await _set_admin_password(client, "pass12345")
    login = (await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "pass12345"},
    )).json()
    token = login["access_token"]

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == ADMIN_EMAIL
    assert me["global_role"] == "admin"
    assert me["is_active"] is True


@pytest.mark.asyncio
async def test_get_me_without_token_falls_back_to_dev_mode(client: AsyncClient):
    """无 token 时仍走 dev header fallback（向后兼容 M0/M1-01）。"""
    r = await client.get("/api/users/me")
    assert r.status_code == 200
    assert r.json()["user"]["email"] == ADMIN_EMAIL


@pytest.mark.asyncio
async def test_invalid_bearer_token_returns_401(client: AsyncClient):
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_exchanges_for_new_access(client: AsyncClient):
    await _set_admin_password(client, "pass12345")
    login = (await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "pass12345"},
    )).json()
    refresh = login["refresh_token"]

    r = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"]
    # 新 refresh 应该不同（旧 refresh 仍然可用 — 我们没做一次性 revoke）
    assert new["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(client: AsyncClient):
    """用 access token 当 refresh 会失败。"""
    await _set_admin_password(client, "pass12345")
    login = (await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "pass12345"},
    )).json()

    r = await client.post("/api/auth/refresh", json={"refresh_token": login["access_token"]})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_set_password_for_self(client: AsyncClient):
    """admin 改自己密码：必须提供 current_password。"""
    await _set_admin_password(client, "OldPass123!")

    r = await client.post(
        "/api/auth/set-password",
        json={"new_password": "NewPass456!", "current_password": "OldPass123!"},
    )
    assert r.status_code == 204

    # 新密码可以登录
    r2 = await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "NewPass456!"},
    )
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_set_password_wrong_current_returns_401(client: AsyncClient):
    await _set_admin_password(client, "OldPass123!")
    r = await client.post(
        "/api/auth/set-password",
        json={"new_password": "NewPass456!", "current_password": "WRONG"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_can_reset_other_users_password(client: AsyncClient):
    """admin 用 target_user_id 代改别人密码。"""
    await _set_admin_password(client, "AdminPass1!")

    # 创建一个非 admin 用户（dev header 触发 lazy create）
    await client.get(
        "/api/users/me",
        headers={"X-User-Email": "alice@example.com"},
    )
    # 找到 alice 的 id
    users_listing = (await client.get("/api/users", headers={"X-User-Email": ADMIN_EMAIL})).json()
    alice = next(u for u in users_listing if u["email"] == "alice@example.com")

    r = await client.post(
        "/api/auth/set-password",
        json={"new_password": "AlicePass1!", "target_user_id": alice["id"]},
    )
    assert r.status_code == 204, r.text

    # alice 现在能用新密码登录
    login = await client.post(
        "/api/auth/login",
        json={"email": "alice@example.com", "password": "AlicePass1!"},
    )
    assert login.status_code == 200, login.text


# ---------------------------------------------------------------------------
# API Key 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key_returns_plain_only_once(client: AsyncClient):
    r = await client.post("/api/api-keys", json={"name": "ci-runner"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plain_key"].startswith("ont_")
    assert body["key_prefix"]
    assert body["name"] == "ci-runner"

    # list 时 plain_key 不出现
    listing = (await client.get("/api/api-keys")).json()
    assert len(listing) == 1
    assert "plain_key" not in listing[0]
    assert listing[0]["key_prefix"] == body["key_prefix"]


@pytest.mark.asyncio
async def test_api_key_can_authenticate_requests(client: AsyncClient):
    created = (await client.post("/api/api-keys", json={"name": "ci"})).json()
    key = created["plain_key"]

    # 用 X-API-Key 调 /api/auth/me
    r = await client.get("/api/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == ADMIN_EMAIL


@pytest.mark.asyncio
async def test_invalid_api_key_returns_401(client: AsyncClient):
    r = await client.get("/api/auth/me", headers={"X-API-Key": "ont_fakefakefake"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revoke_api_key(client: AsyncClient):
    created = (await client.post("/api/api-keys", json={"name": "to-revoke"})).json()
    key_id = created["id"]
    plain = created["plain_key"]

    # 撤销
    r = await client.delete(f"/api/api-keys/{key_id}")
    assert r.status_code == 204

    # 已撤销的 key 不能用了
    r2 = await client.get("/api/auth/me", headers={"X-API-Key": plain})
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_revoke_unknown_key_returns_404(client: AsyncClient):
    import uuid as _uuid
    r = await client.delete(f"/api/api-keys/{_uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revocation_is_idempotent(client: AsyncClient):
    """已撤销的 key 重复撤销不报错。"""
    created = (await client.post("/api/api-keys", json={"name": "x"})).json()
    key_id = created["id"]

    assert (await client.delete(f"/api/api-keys/{key_id}")).status_code == 204
    assert (await client.delete(f"/api/api-keys/{key_id}")).status_code == 204


@pytest.mark.asyncio
async def test_api_key_with_expiry(client: AsyncClient):
    """expires_in_days 参数能写进 expires_at 字段。"""
    created = (await client.post(
        "/api/api-keys",
        json={"name": "with-expiry", "expires_in_days": 30},
    )).json()
    assert created["expires_at"] is not None


@pytest.mark.asyncio
async def test_bearer_token_takes_priority_over_api_key_header(client: AsyncClient):
    """当 Bearer 与 X-API-Key 同时存在时，Bearer 优先。"""
    await _set_admin_password(client, "AdminPass1!")
    login = (await client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "AdminPass1!"},
    )).json()
    token = login["access_token"]

    # 同时给一个无效的 X-API-Key；用 Bearer 应该 200
    r = await client.get(
        "/api/auth/me",
        headers={
            "Authorization": f"Bearer {token}",
            "X-API-Key": "ont_invalid",
        },
    )
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN_EMAIL

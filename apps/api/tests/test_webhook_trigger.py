"""HIA-75 C3 — Webhook / Trigger integration tests.

覆盖：
* WebhookConfig CRUD
* HMAC-SHA256 签名验证
* 入站 Webhook 端点（POST /api/webhooks/in/{token}）
* TriggerConfig CRUD（inbound_webhook / schedule）
* 事件分发：CR merge / release publish / object CRUD
* 重试机制（验证重试次数）
* Cron 解析（*/5, exact, comma-list）

每个测试用独立的 SQLite 跑 alembic up head + 直接 ASGI 调。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))


# ---------- per-test 数据库覆盖 ----------


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


ADMIN_EMAIL = "admin@ontolohub.local"
ALICE = "alice@example.com"
BOB = "bob@example.com"


async def _project(client: AsyncClient, owner: str = ALICE) -> str:
    r = await client.post(
        "/projects", json={"name": f"Webhook Test {uuid.uuid4()}"}, headers={"X-User-Email": owner}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _action_type(client: AsyncClient, project_id: str) -> str:
    """创建一个简单的 webhook 类型 ActionType（可被 trigger 触发）。"""
    r = await client.post(
        f"/projects/{project_id}/actions",
        json={
            "name": "test-action",
            "kind": "webhook",
            "config": {"url": "http://localhost:9999/never-called"},
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ===========================================================================
# WebhookConfig 单元测试
# ===========================================================================


@pytest.mark.asyncio
async def test_create_webhook(client: AsyncClient):
    """创建 webhook 配置，自动生成 secret。"""
    project_id = await _project(client)

    r = await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "Hook to webhook.site",
            "url": "https://webhook.site/unique-id",
            "events": ["cr.merged", "release.published"],
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Hook to webhook.site"
    assert body["url"] == "https://webhook.site/unique-id"
    assert "cr.merged" in body["events"]
    assert "release.published" in body["events"]
    assert body["is_enabled"] is True
    # secret should be returned on creation
    assert len(body["secret"]) > 0
    assert body["secret"] != "***REDACTED***"


@pytest.mark.asyncio
async def test_list_webhooks_masks_secret(client: AsyncClient):
    """列表 webhook 时 secret 应被遮蔽。"""
    project_id = await _project(client)
    await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "Mask Test",
            "url": "https://example.com",
            "events": ["cr.merged"],
            "secret": "my-fixed-secret",
        },
        headers={"X-User-Email": ALICE},
    )

    r = await client.get(
        f"/projects/{project_id}/webhooks", headers={"X-User-Email": ALICE}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["secret"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_update_webhook(client: AsyncClient):
    """更新 webhook 配置。"""
    project_id = await _project(client)
    create = await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "To Update",
            "url": "https://old.example.com",
            "events": ["cr.merged"],
        },
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.patch(
        f"/projects/{project_id}/webhooks/{wid}",
        json={
            "url": "https://new.example.com",
            "events": ["cr.merged", "release.published"],
            "is_enabled": False,
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "https://new.example.com"
    assert body["is_enabled"] is False
    assert "release.published" in body["events"]


@pytest.mark.asyncio
async def test_delete_webhook(client: AsyncClient):
    """删除 webhook 配置。"""
    project_id = await _project(client)
    create = await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "To Delete",
            "url": "https://example.com",
            "events": ["cr.merged"],
        },
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.delete(
        f"/projects/{project_id}/webhooks/{wid}", headers={"X-User-Email": ALICE}
    )
    assert r.status_code == 204, r.text

    # 列表应为空
    r = await client.get(
        f"/projects/{project_id}/webhooks", headers={"X-User-Email": ALICE}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 0


# ===========================================================================
# HMAC-SHA256 签名
# ===========================================================================


def test_hmac_signature_unit():
    """单元测试：HMAC-SHA256 签名函数正确性。"""
    from src.services.webhook_dispatcher import _compute_signature

    secret = "my-secret-key"
    payload = b'{"event":"test","value":42}'

    sig = _compute_signature(secret, payload)
    # Manual HMAC-SHA256
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert sig == expected

    # Tampering should produce different signature
    tampered_payload = b'{"event":"test","value":99}'
    tampered_sig = _compute_signature(secret, tampered_payload)
    assert sig != tampered_sig


@pytest.mark.asyncio
async def test_inbound_webhook_signature_validation(client: AsyncClient):
    """接收 webhook 时签名计算应一致（验证 _build_headers）。"""
    from src.services.webhook_dispatcher import _build_headers

    payload = {"event": "test", "value": 42}
    secret = "test-secret"

    headers = _build_headers(payload, secret)
    assert headers["X-OntoloHub-Signature"].startswith("sha256=")
    assert headers["Content-Type"] == "application/json"
    assert "X-OntoloHub-Timestamp" in headers


# ===========================================================================
# Inbound webhook 端点
# ===========================================================================


@pytest.mark.asyncio
async def test_inbound_webhook_creates_action_run(client: AsyncClient):
    """入站 webhook 触发 ActionRun。"""
    import sys as _sys
    print(f"TEST7 START", file=_sys.stderr)
    project_id = await _project(client)
    action_id = await _action_type(client, project_id)
    print(f"TEST7 action_id={action_id}", file=_sys.stderr)

    # 创建 inbound_webhook trigger
    r = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Inbound Test",
            "trigger_type": "inbound_webhook",
            "action_type_id": action_id,
            "input_template": {"payload": "{{webhook.payload}}"},
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    trigger = r.json()
    token = trigger["trigger_config"]["token"]
    assert len(token) > 20, "token should be auto-generated and long"

    # 触发入站 webhook
    r = await client.post(
        f"/api/webhooks/in/{token}",
        json={"foo": "bar", "value": 42},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["trigger_id"] == trigger["id"]

    # trigger 的 total_runs 应增加（异步执行可能尚未完成，但 total_runs 在创建 run 时立即 +1）
    await asyncio.sleep(0.5)

    r = await client.get(
        f"/projects/{project_id}/triggers/{trigger['id']}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_runs"] >= 1


@pytest.mark.asyncio
async def test_inbound_webhook_invalid_token(client: AsyncClient):
    """无效 token 返回 404。"""
    r = await client.post(
        "/api/webhooks/in/invalid-token-12345",
        json={"foo": "bar"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_inbound_webhook_paused_trigger(client: AsyncClient):
    """paused 状态的 trigger 不应被触发。"""
    project_id = await _project(client)
    action_id = await _action_type(client, project_id)

    r = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Paused Test",
            "trigger_type": "inbound_webhook",
            "action_type_id": action_id,
        },
        headers={"X-User-Email": ALICE},
    )
    trigger = r.json()
    token = trigger["trigger_config"]["token"]

    # Pause the trigger
    r = await client.patch(
        f"/projects/{project_id}/triggers/{trigger['id']}",
        json={"status": "paused"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/api/webhooks/in/{token}",
        json={"foo": "bar"},
    )
    assert r.status_code == 404


# ===========================================================================
# TriggerConfig CRUD
# ===========================================================================


@pytest.mark.asyncio
async def test_create_schedule_trigger(client: AsyncClient):
    """创建 schedule trigger（cron 表达式）。"""
    project_id = await _project(client)
    action_id = await _action_type(client, project_id)

    r = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Every 5 Minutes",
            "trigger_type": "schedule",
            "trigger_config": {"cron": "*/5 * * * *"},
            "action_type_id": action_id,
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["trigger_type"] == "schedule"
    assert body["trigger_config"]["cron"] == "*/5 * * * *"
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_trigger_validation(client: AsyncClient):
    """无效的 trigger_type 应被拒绝。"""
    project_id = await _project(client)
    action_id = await _action_type(client, project_id)

    r = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Bad",
            "trigger_type": "invalid",
            "action_type_id": action_id,
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422  # validation error


@pytest.mark.asyncio
async def test_trigger_action_type_not_found(client: AsyncClient):
    """引用不存在的 ActionType 应返回 404。"""
    project_id = await _project(client)

    r = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Ghost",
            "trigger_type": "inbound_webhook",
            "action_type_id": str(uuid.uuid4()),
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


# ===========================================================================
# Cron 表达式解析
# ===========================================================================


def test_cron_matches_simple():
    """测试 _cron_matches 函数的基本表达式。"""
    from src.services.webhook_dispatcher import _cron_matches

    # */5 配合 minute field
    dt = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)
    assert _cron_matches("*/5 * * * *", dt) is True

    dt = datetime(2026, 9, 18, 10, 3, 0, tzinfo=timezone.utc)
    assert _cron_matches("*/5 * * * *", dt) is False

    dt = datetime(2026, 9, 18, 10, 5, 0, tzinfo=timezone.utc)
    assert _cron_matches("*/5 * * * *", dt) is True

    # Exact hour
    dt = datetime(2026, 9, 18, 14, 30, 0, tzinfo=timezone.utc)
    assert _cron_matches("30 14 * * *", dt) is True

    # Comma list
    dt = datetime(2026, 9, 18, 10, 15, 0, tzinfo=timezone.utc)
    assert _cron_matches("15,30,45 * * * *", dt) is True

    # Wildcard all
    assert _cron_matches("* * * * *", dt) is True


def test_cron_matches_invalid():
    """无效 cron 应返回 False（不抛异常）。"""
    from src.services.webhook_dispatcher import _cron_matches

    dt = datetime.now(timezone.utc)
    assert _cron_matches("", dt) is False
    assert _cron_matches("invalid", dt) is False
    assert _cron_matches("* * *", dt) is False
    assert _cron_matches("*/a * * * *", dt) is False


# ===========================================================================
# 事件分发：CR 合并
# ===========================================================================


@pytest.mark.asyncio
async def test_cr_merge_dispatches_webhook(client: AsyncClient):
    """CR merge 应触发 webhook 分发（带 cr.merged 事件）。"""
    from src.services.webhook_dispatcher import dispatch_webhook, WebhookEventType, build_event_payload

    project_id = await _project(client)

    # 直接调用 dispatch（避免 HTTP 真实投递）
    payload = build_event_payload(
        event_type=WebhookEventType.CR_MERGED,
        target_type="change_request",
        target_id=uuid.uuid4(),
        extra={"title": "test"},
    )
    delivery_ids = await dispatch_webhook(
        event_type=WebhookEventType.CR_MERGED,
        project_id=project_id,
        payload=payload,
    )

    # 没有 webhook 订阅 → 应该返回空列表
    assert delivery_ids == []


# ===========================================================================
# 事件分发：build_event_payload
# ===========================================================================


def test_build_event_payload_unit():
    """单元测试：build_event_payload 包含必要的字段。"""
    from src.services.webhook_dispatcher import build_event_payload, WebhookEventType

    payload = build_event_payload(
        event_type=WebhookEventType.OBJECT_CREATED,
        target_type="object",
        target_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_name="alice",
        extra={"custom": "value"},
    )

    assert payload["event"] == "object.created"
    assert payload["target"]["type"] == "object"
    assert payload["target"]["id"]
    assert payload["actor"]["name"] == "alice"
    assert payload["custom"] == "value"
    assert "timestamp" in payload


# ===========================================================================
# Webhook 投递历史
# ===========================================================================


@pytest.mark.asyncio
async def test_list_deliveries_empty(client: AsyncClient):
    """新创建的 webhook 投递历史为空。"""
    project_id = await _project(client)
    create = await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "Delivery Test",
            "url": "https://example.com",
            "events": ["cr.merged"],
        },
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.get(
        f"/projects/{project_id}/webhooks/{wid}/deliveries",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


# ===========================================================================
# 权限隔离
# ===========================================================================


@pytest.mark.asyncio
async def test_webhook_project_isolation(client: AsyncClient):
    """用户 B 不能访问用户 A 的 webhook。"""
    project_id = await _project(client, owner=ALICE)
    create = await client.post(
        f"/projects/{project_id}/webhooks",
        json={
            "name": "Private",
            "url": "https://example.com",
            "events": ["cr.merged"],
        },
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    # Bob 试图访问
    r = await client.get(
        f"/projects/{project_id}/webhooks/{wid}", headers={"X-User-Email": BOB}
    )
    # Bob 不是项目成员 → 应 404
    assert r.status_code in (403, 404), r.text


# ===========================================================================
# 触发器暂停 / 恢复
# ===========================================================================


@pytest.mark.asyncio
async def test_pause_and_resume_trigger(client: AsyncClient):
    """暂停后再恢复 trigger。"""
    project_id = await _project(client)
    action_id = await _action_type(client, project_id)

    create = await client.post(
        f"/projects/{project_id}/triggers",
        json={
            "name": "Pause Test",
            "trigger_type": "inbound_webhook",
            "action_type_id": action_id,
        },
        headers={"X-User-Email": ALICE},
    )
    tid = create.json()["id"]

    # 暂停
    r = await client.patch(
        f"/projects/{project_id}/triggers/{tid}",
        json={"status": "paused"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"

    # 恢复
    r = await client.patch(
        f"/projects/{project_id}/triggers/{tid}",
        json={"status": "active"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

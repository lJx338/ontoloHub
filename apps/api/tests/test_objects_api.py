"""HIA-59 集成测试 — Object & Link API。

覆盖：
- Object CRUD（创建 / 获取 / 列表 / 更新 / 软删除）
- identity_key upsert 行为
- 跨项目隔离（404 而非 403，无侧信道）
- ontology class IRI 可见性校验
- Link CRUD + 重复 (source, target, relation_iri) upsert
- Bulk upsert 行为
- 审计日志写入
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))


# ---------- per-test 数据库覆盖 ----------

@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test_objects.db"
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

EMAIL_ADMIN = "admin@ontolohub.local"
EMAIL_ALICE = "alice@example.com"
EMAIL_BOB = "bob@example.com"


async def _alice_project(client: AsyncClient) -> str:
    headers = {"X-User-Email": EMAIL_ALICE}
    r = await client.post("/projects", json={"name": "Alice P"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _bob_project(client: AsyncClient) -> str:
    headers = {"X-User-Email": EMAIL_BOB}
    r = await client.post("/projects", json={"name": "Bob P"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_ontology(client: AsyncClient, proj: str, iri: str, name: str) -> str:
    """创建 reference ontology + 一个类（用 project_id 也行；用 reference 不绑 project 较简单）。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    r = await client.post(
        "/ontologies",
        headers=alice_h,
        json={
            "name": name,
            "namespace": "http://example.org/",
            "kind": "reference",
            "standard_name": name,
        },
    )
    assert r.status_code == 201, r.text
    onto_id = r.json()["id"]

    r = await client.post(
        f"/ontologies/{onto_id}/classes",
        headers=alice_h,
        json={"name": name, "iri": iri, "class_type": "ontology_class"},
    )
    assert r.status_code == 201, r.text
    return onto_id


# ============================================================
# Object CRUD
# ============================================================


@pytest.mark.asyncio
async def test_create_object_basic(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={
            "name": "Alice",
            "ontology_class_iri": "http://example.org/Person",
            "data": {"email": "alice@example.com"},
            "identity_key": "person:alice@example.com",
        },
    )
    assert r.status_code == 201, r.text
    obj = r.json()
    assert obj["name"] == "Alice"
    assert obj["ontology_class_iri"] == "http://example.org/Person"
    assert obj["data"]["email"] == "alice@example.com"
    assert obj["status"] == "active"
    assert obj["identity_key"] == "person:alice@example.com"


@pytest.mark.asyncio
async def test_create_object_identity_key_upsert(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    # 第一次创建
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={
            "name": "Alice v1",
            "ontology_class_iri": "http://example.org/Person",
            "data": {"v": 1},
            "identity_key": "k1",
        },
    )
    assert r.status_code == 201
    oid = r.json()["id"]

    # 同 identity_key 应更新而非新建
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={
            "name": "Alice v2",
            "ontology_class_iri": "http://example.org/Person",
            "data": {"v": 2},
            "identity_key": "k1",
        },
    )
    assert r.status_code == 201
    assert r.json()["id"] == oid
    assert r.json()["name"] == "Alice v2"
    assert r.json()["data"]["v"] == 2


@pytest.mark.asyncio
async def test_get_object(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "x", "ontology_class_iri": "http://example.org/X"},
    )
    oid = r.json()["id"]
    r = await client.get(f"/objects/projects/{proj}/objects/{oid}", headers=alice_h)
    assert r.status_code == 200
    assert r.json()["id"] == oid


@pytest.mark.asyncio
async def test_list_objects_with_filters(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    for i, cls in enumerate(["http://example.org/A", "http://example.org/B", "http://example.org/A"]):
        await client.post(
            f"/objects/projects/{proj}/objects",
            headers=alice_h,
            json={"name": f"o{i}", "ontology_class_iri": cls},
        )

    r = await client.get(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        params={"class_iri": "http://example.org/A"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2

    r = await client.get(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        params={"q": "o1"},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["name"] == "o1"


@pytest.mark.asyncio
async def test_update_object(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "x", "data": {"a": 1}, "ontology_class_iri": "http://example.org/X"},
    )
    oid = r.json()["id"]
    r = await client.patch(
        f"/objects/projects/{proj}/objects/{oid}",
        headers=alice_h,
        json={"name": "x2", "data": {"a": 2, "b": 3}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "x2"
    assert body["data"]["a"] == 2
    assert body["data"]["b"] == 3


@pytest.mark.asyncio
async def test_delete_object_soft(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "x", "ontology_class_iri": "http://example.org/X"},
    )
    oid = r.json()["id"]
    r = await client.delete(
        f"/objects/projects/{proj}/objects/{oid}", headers=alice_h
    )
    assert r.status_code == 204

    # 软删除后默认 list 不可见
    r = await client.get(
        f"/objects/projects/{proj}/objects", headers=alice_h
    )
    assert r.json()["total"] == 0

    # 直接 get 应 404
    r = await client.get(
        f"/objects/projects/{proj}/objects/{oid}", headers=alice_h
    )
    assert r.status_code == 404


# ============================================================
# 跨项目隔离
# ============================================================


@pytest.mark.asyncio
async def test_cross_project_isolation_returns_404(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    bob_h = {"X-User-Email": EMAIL_BOB}
    proj_alice = await _alice_project(client)
    proj_bob = await _bob_project(client)

    # Alice 在自己项目下创建对象
    r = await client.post(
        f"/objects/projects/{proj_alice}/objects",
        headers=alice_h,
        json={"name": "secret", "ontology_class_iri": "http://example.org/X"},
    )
    oid = r.json()["id"]

    # Bob 用自己项目 ID 试图访问 → 必须 404（无侧信道）
    r = await client.get(
        f"/objects/projects/{proj_bob}/objects/{oid}", headers=bob_h
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_create_returns_404(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    # Bob 试图在 Alice 项目创建 → 应 404（非 403，避免项目存在侧信道）
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers={"X-User-Email": EMAIL_BOB},
        json={"name": "x", "ontology_class_iri": "http://example.org/X"},
    )
    assert r.status_code == 404


# ============================================================
# 本体 class 引用校验
# ============================================================


@pytest.mark.asyncio
async def test_object_with_unknown_class_id_400(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={
            "name": "x",
            "ontology_class_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_object_with_invisible_class_iri_falls_back_to_forward_compat(client: AsyncClient):
    """project-private ontology 的 class IRI 在跨项目时也允许存储（forward-compat）。

    M0 阶段：找不到本体绑定时不报错，原样存储 IRI，方便 ETL 场景先有数据
    再有 ontology。M1+ 可在 SHACL 校验时再发现 unbound IRI 即可。
    """
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj_alice = await _alice_project(client)
    proj_bob = await _bob_project(client)

    # Alice 创建 project-private ontology + class
    r = await client.post(
        "/ontologies",
        headers=alice_h,
        json={
            "name": "Alice Private",
            "namespace": "http://alice-private/",
            "kind": "project",
            "project_id": proj_alice,
        },
    )
    assert r.status_code == 201, r.text
    onto_id = r.json()["id"]
    r = await client.post(
        f"/ontologies/{onto_id}/classes",
        headers=alice_h,
        json={
            "name": "AliceClass",
            "iri": "http://alice-private/AliceClass",
            "class_type": "ontology_class",
        },
    )
    assert r.status_code == 201, r.text

    # Alice 在自己项目里用该 IRI 创建对象 → OK
    r = await client.post(
        f"/objects/projects/{proj_alice}/objects",
        headers=alice_h,
        json={"name": "x", "ontology_class_iri": "http://alice-private/AliceClass"},
    )
    assert r.status_code == 201

    # Bob 在自己项目里用同样 IRI → 也 OK（forward-compat：原样存储）
    bob_h = {"X-User-Email": EMAIL_BOB}
    r = await client.post(
        f"/objects/projects/{proj_bob}/objects",
        headers=bob_h,
        json={"name": "x", "ontology_class_iri": "http://alice-private/AliceClass"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ontology_class_iri"] == "http://alice-private/AliceClass"
    # ontology_class_id 仍为 None，因为没有 binding
    assert body["ontology_class_id"] is None


@pytest.mark.asyncio
async def test_reference_ontology_visible_to_any_project(client: AsyncClient):
    """reference ontology 全局可见。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    bob_h = {"X-User-Email": EMAIL_BOB}

    # Alice 创建 reference ontology
    r = await client.post(
        "/ontologies",
        headers=alice_h,
        json={
            "name": "Global",
            "namespace": "http://global/",
            "kind": "reference",
        },
    )
    onto_id = r.json()["id"]
    r = await client.post(
        f"/ontologies/{onto_id}/classes",
        headers=alice_h,
        json={
            "name": "Person",
            "iri": "http://global/Person",
            "class_type": "ontology_class",
        },
    )
    assert r.status_code == 201

    # Bob 能用
    proj_bob = await _bob_project(client)
    r = await client.post(
        f"/objects/projects/{proj_bob}/objects",
        headers=bob_h,
        json={"name": "Bob's person", "ontology_class_iri": "http://global/Person"},
    )
    assert r.status_code == 201


# ============================================================
# Bulk upsert
# ============================================================


@pytest.mark.asyncio
async def test_bulk_upsert_insert_then_update(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    r = await client.post(
        f"/objects/projects/{proj}/objects/bulk-upsert",
        headers=alice_h,
        json={
            "items": [
                {
                    "name": "A",
                    "ontology_class_iri": "http://example.org/A",
                    "data": {"x": 1},
                    "identity_key": "a1",
                },
                {
                    "name": "B",
                    "ontology_class_iri": "http://example.org/B",
                    "data": {"x": 2},
                    "identity_key": "b1",
                },
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 2
    assert body["updated"] == 0
    assert body["failed"] == 0

    # 再来：a1 应被更新（merge），b1 同上；新增 c1
    r = await client.post(
        f"/objects/projects/{proj}/objects/bulk-upsert",
        headers=alice_h,
        json={
            "items": [
                {
                    "name": "A",
                    "ontology_class_iri": "http://example.org/A",
                    "data": {"y": 100},  # merge
                    "identity_key": "a1",
                },
                {
                    "name": "C",
                    "ontology_class_iri": "http://example.org/C",
                    "data": {"z": 3},
                    "identity_key": "c1",
                },
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 1
    assert body["updated"] == 1
    assert body["failed"] == 0

    # 验证 a1 的 data 合并了 x 和 y
    r = await client.get(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        params={"q": "a1"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["data"]["x"] == 1
    assert items[0]["data"]["y"] == 100


@pytest.mark.asyncio
async def test_bulk_upsert_replace_true(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    await client.post(
        f"/objects/projects/{proj}/objects/bulk-upsert",
        headers=alice_h,
        json={
            "items": [
                {
                    "name": "A",
                    "ontology_class_iri": "http://example.org/A",
                    "data": {"x": 1, "y": 2},
                    "identity_key": "k1",
                }
            ]
        },
    )

    # replace=True → 整个 data 被替换
    r = await client.post(
        f"/objects/projects/{proj}/objects/bulk-upsert",
        headers=alice_h,
        json={"replace": True, "items": [
            {
                "name": "A2",
                "ontology_class_iri": "http://example.org/A",
                "data": {"z": 99},
                "identity_key": "k1",
            }
        ]},
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    r = await client.get(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        params={"q": "k1"},
    )
    obj = r.json()["items"][0]
    assert "x" not in obj["data"]
    assert obj["data"]["z"] == 99


# ============================================================
# Link CRUD
# ============================================================


@pytest.mark.asyncio
async def test_link_lifecycle_and_dedup(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    # 创建 source / target
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "src", "ontology_class_iri": "http://example.org/A"},
    )
    src = r.json()["id"]
    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "tgt", "ontology_class_iri": "http://example.org/B"},
    )
    tgt = r.json()["id"]

    # 创建 link
    r = await client.post(
        f"/objects/projects/{proj}/links",
        headers=alice_h,
        json={
            "source_id": src,
            "target_id": tgt,
            "ontology_relation_iri": "http://example.org/rel",
            "link_type": "association",
        },
    )
    assert r.status_code == 201
    link_id = r.json()["id"]

    # 重复 (source, target, relation_iri) 应 upsert，不新增
    r = await client.post(
        f"/objects/projects/{proj}/links",
        headers=alice_h,
        json={
            "source_id": src,
            "target_id": tgt,
            "ontology_relation_iri": "http://example.org/rel",
            "link_type": "composition",
            "properties": {"weight": 0.8},
        },
    )
    assert r.status_code == 201
    assert r.json()["id"] == link_id
    assert r.json()["link_type"] == "composition"
    assert r.json()["properties"]["weight"] == 0.8

    # list
    r = await client.get(
        f"/objects/projects/{proj}/links",
        headers=alice_h,
        params={"source_id": src},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1

    # get
    r = await client.get(
        f"/objects/projects/{proj}/links/{link_id}", headers=alice_h
    )
    assert r.status_code == 200

    # patch
    r = await client.patch(
        f"/objects/projects/{proj}/links/{link_id}",
        headers=alice_h,
        json={"confidence": 0.42},
    )
    assert r.status_code == 200
    assert r.json()["confidence"] == 0.42

    # delete
    r = await client.delete(
        f"/objects/projects/{proj}/links/{link_id}", headers=alice_h
    )
    assert r.status_code == 204

    # 删后 get 应 404
    r = await client.get(
        f"/objects/projects/{proj}/links/{link_id}", headers=alice_h
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_link_cross_project_source_rejected(client: AsyncClient):
    """链接 source/target 必须同 project。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    bob_h = {"X-User-Email": EMAIL_BOB}

    proj_alice = await _alice_project(client)
    proj_bob = await _bob_project(client)

    r = await client.post(
        f"/objects/projects/{proj_alice}/objects",
        headers=alice_h,
        json={"name": "src", "ontology_class_iri": "http://example.org/A"},
    )
    src_alice = r.json()["id"]
    r = await client.post(
        f"/objects/projects/{proj_bob}/objects",
        headers=bob_h,
        json={"name": "tgt", "ontology_class_iri": "http://example.org/B"},
    )
    tgt_bob = r.json()["id"]

    # Alice 在自己项目里尝试链接到 Bob 的对象 → 应 404（target 在 Alice 项目不可见）
    r = await client.post(
        f"/objects/projects/{proj_alice}/links",
        headers=alice_h,
        json={
            "source_id": src_alice,
            "target_id": tgt_bob,
        },
    )
    assert r.status_code == 404


# ============================================================
# 审计写入
# ============================================================


@pytest.mark.asyncio
async def test_audit_written_for_object_operations(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    r = await client.post(
        f"/objects/projects/{proj}/objects",
        headers=alice_h,
        json={"name": "x", "ontology_class_iri": "http://example.org/X"},
    )
    oid = r.json()["id"]

    await client.patch(
        f"/objects/projects/{proj}/objects/{oid}",
        headers=alice_h,
        json={"name": "x2"},
    )
    await client.delete(
        f"/objects/projects/{proj}/objects/{oid}", headers=alice_h
    )

    # 拉项目审计（viewer 可读）
    r = await client.get(
        f"/projects/{proj}/audit", headers=alice_h
    )
    assert r.status_code == 200
    events = r.json()
    types = [e["event_type"] for e in events]
    assert "create" in types
    assert "update" in types
    assert "delete" in types
    # 都应该是 object 类型的 target
    assert all(e.get("target_type") == "object" for e in events if e["event_type"] in ("created", "updated", "deleted"))

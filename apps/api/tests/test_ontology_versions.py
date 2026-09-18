"""HIA-56 / A7 Ontology Version CRUD 集成测试。

覆盖：
* POST /ontologies/{id}/versions — 基于 head 创建草稿版本
* PUT /ontologies/{id}/versions/{vid}/content — 更新版本快照内容
* POST /ontologies/{id}/versions/{vid}/publish — 发布特定版本为 head
* GET /ontologies/{id}/versions — 列表（含 diff）

每个测试用独立的 SQLite 跑 alembic up head + 直接 ASGI 调。
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

ALICE = "alice@ontolohub.local"


async def _project(client: AsyncClient) -> str:
    r = await client.post(
        "/projects",
        json={"name": f"HIA-56 Test {uuid.uuid4()}"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _ontology(client: AsyncClient, project_id: str) -> tuple[str, str]:
    """创建 ontology + publish → 返回 (ontology_id, version_id)。"""
    r = await client.post(
        "/ontologies",
        params={"project_id": project_id},
        json={
            "name": "TestOnto",
            "namespace": "http://test.example/onto#",
            "description": "HIA-56 test",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    onto_id = r.json()["id"]

    # publish → 生成 v0.1.0
    r = await client.post(
        f"/ontologies/{onto_id}/publish",
        params={"project_id": project_id},
        json={"version": "v0.1.0", "change_summary": "initial"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    v0_id = r.json()["version_id"]
    return onto_id, v0_id


# ===================================================================
# Version CRUD
# ===================================================================


@pytest.mark.asyncio
async def test_list_versions_empty(client: AsyncClient):
    """新本体无版本时列表为空。"""
    project_id = await _project(client)
    r = await client.post(
        "/ontologies",
        params={"project_id": project_id},
        json={"name": "EmptyOnto", "namespace": "http://empty/onto#"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    onto_id = r.json()["id"]

    r = await client.get(f"/ontologies/{onto_id}/versions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_draft_version(client: AsyncClient):
    """POST /ontologies/{id}/versions 基于 head 创建草稿。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # 创建草稿版本（默认 "draft" 标签）
    r = await client.post(
        f"/ontologies/{onto_id}/versions",
        params={"project_id": project_id},
        json={"version": "draft-1", "change_summary": "work in progress"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == "draft-1"
    assert body["status"] == "draft"
    assert body["class_snapshot"] == []  # 从 v0.1.0 继承（空快照）
    assert body["class_count"] == 0
    assert "id" in body
    assert "ontology_id" in body


@pytest.mark.asyncio
async def test_create_draft_version_inherits_snapshots(client: AsyncClient):
    """草稿版本从最新 published 版本继承快照内容。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # 创建草稿 → 应继承 v0.1.0 的快照
    r = await client.post(
        f"/ontologies/{onto_id}/versions",
        params={"project_id": project_id},
        json={"version": "draft-from-v2"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == "draft-from-v2"
    assert body["status"] == "draft"
    # v0.1.0 发布时没有实际类数据，所以快照为空
    assert body["class_snapshot"] == []


@pytest.mark.asyncio
async def test_update_version_content(client: AsyncClient):
    """PUT /ontologies/{id}/versions/{vid}/content 更新快照内容。"""
    project_id = await _project(client)
    onto_id, _ = await _ontology(client, project_id)

    # 创建草稿
    r = await client.post(
        f"/ontologies/{onto_id}/versions",
        params={"project_id": project_id},
        json={"version": "draft-content"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    vid = r.json()["id"]

    # 更新快照内容
    new_class_snapshot = [
        {
            "iri": "http://test.example/onto#Person",
            "name": "Person",
            "description": "Represents a person",
        }
    ]
    new_prop_snapshot = [
        {
            "iri": "http://test.example/onto#name",
            "name": "name",
            "data_type": "string",
        }
    ]
    r = await client.put(
        f"/ontologies/{onto_id}/versions/{vid}/content",
        params={"project_id": project_id},
        json={
            "class_snapshot": new_class_snapshot,
            "property_snapshot": new_prop_snapshot,
            "change_summary": "added Person class",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["class_count"] == 1
    assert body["property_count"] == 1
    assert body["class_snapshot"][0]["name"] == "Person"
    assert body["property_snapshot"][0]["name"] == "name"


@pytest.mark.asyncio
async def test_update_version_content_rejects_published(client: AsyncClient):
    """已发布的版本不允许更新内容。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # v0_id 是已发布版本
    r = await client.put(
        f"/ontologies/{onto_id}/versions/{v0_id}/content",
        params={"project_id": project_id},
        json={"class_snapshot": [{"name": "X"}]},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 400, r.text
    assert "DRAFT" in r.json()["detail"]


@pytest.mark.asyncio
async def test_publish_version(client: AsyncClient):
    """POST /ontologies/{id}/versions/{vid}/publish 发布草稿为 head。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # 创建草稿
    r = await client.post(
        f"/ontologies/{onto_id}/versions",
        params={"project_id": project_id},
        json={"version": "draft-publish"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    vid = r.json()["id"]

    # 发布它
    r = await client.post(
        f"/ontologies/{onto_id}/versions/{vid}/publish",
        params={"project_id": project_id},
        json={"change_summary": "ready for prod"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "draft-publish"
    assert body["version_id"] == vid
    assert body["published_at"]

    # 列表验证状态
    r = await client.get(f"/ontologies/{onto_id}/versions")
    assert r.status_code == 200
    versions = {v["version"]: v for v in r.json()}
    assert versions["draft-publish"]["status"] == "published"
    assert versions["v0.1.0"]["status"] == "published"


@pytest.mark.asyncio
async def test_publish_version_rejects_published(client: AsyncClient):
    """重复发布已发布的版本应 400。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # v0_id 已是 published
    r = await client.post(
        f"/ontologies/{onto_id}/versions/{v0_id}/publish",
        params={"project_id": project_id},
        json={"change_summary": "try again"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 400, r.text
    assert "DRAFT" in r.json()["detail"]


@pytest.mark.asyncio
async def test_get_version_404(client: AsyncClient):
    """访问不存在的版本 → 404。"""
    project_id = await _project(client)
    onto_id, _ = await _ontology(client, project_id)

    r = await client.get(
        f"/ontologies/{onto_id}/versions/{uuid.uuid4()}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_content_404(client: AsyncClient):
    """更新不存在的版本内容 → 404。"""
    project_id = await _project(client)
    onto_id, _ = await _ontology(client, project_id)

    r = await client.put(
        f"/ontologies/{onto_id}/versions/{uuid.uuid4()}/content",
        params={"project_id": project_id},
        json={"class_snapshot": []},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_diff_endpoint(client: AsyncClient):
    """GET /ontologies/{id}/diff 对比两个版本。"""
    project_id = await _project(client)
    onto_id, v0_id = await _ontology(client, project_id)

    # 发布 v0.2.0：先创建草稿再通过新端点发布
    r = await client.post(
        f"/ontologies/{onto_id}/versions",
        params={"project_id": project_id},
        json={"version": "v0.2.0"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    draft_v1_id = r.json()["id"]

    r = await client.post(
        f"/ontologies/{onto_id}/versions/{draft_v1_id}/publish",
        params={"project_id": project_id},
        json={"change_summary": "second release"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200

    # diff
    r = await client.get(
        f"/ontologies/{onto_id}/diff",
        params={"from_version": "v0.1.0", "to_version": "v0.2.0"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_version"] == "v0.1.0"
    assert body["to_version"] == "v0.2.0"
    assert "summary" in body
    assert "classes" in body["summary"]
    assert "properties" in body["summary"]


@pytest.mark.asyncio
async def test_diff_nonexistent_version_404(client: AsyncClient):
    """diff 不存在的版本 → 404。"""
    project_id = await _project(client)
    onto_id, _ = await _ontology(client, project_id)

    r = await client.get(
        f"/ontologies/{onto_id}/diff",
        params={"from_version": "v0.1.0", "to_version": "nonexistent"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_fork_endpoint(client: AsyncClient):
    """POST /ontologies/{id}/fork 创建本体副本（不同 namespace）。"""
    project_id = await _project(client)
    onto_id, _ = await _ontology(client, project_id)

    r = await client.post(
        f"/ontologies/{onto_id}/fork",
        params={"project_id": project_id},
        json={
            "name": "ForkedOnto",
            "namespace": "http://fork.example/onto#",
            "description": "A fork",
            "version_tag": "fork-v1",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["forked_ontology_name"] == "ForkedOnto"
    assert body["forked_namespace"] == "http://fork.example/onto#"
    assert body["parent_ontology_id"] == onto_id
    assert "forked_ontology_id" in body
    assert "forked_version_id" in body

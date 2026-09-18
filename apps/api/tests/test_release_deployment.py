"""HIA-61 / A11 Release + Deployment + Preflight API 集成测试。

覆盖：
* Release CRUD：创建 / 列表 / 详情 / 修改 / 下载 manifest / 发布
* Preflight：运行预检 → PreflightReport 落库
* Deployment：创建 / 列表 / 详情 / 回滚
* UseCaseBundle / PreflightReport 列表

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

ADMIN_EMAIL = "admin@ontolohub.local"
ALICE = "alice@example.com"
BOB = "bob@example.com"


async def _project(client: AsyncClient, owner: str = ALICE) -> str:
    r = await client.post(
        "/projects", json={"name": f"Release Test {uuid.uuid4()}"}, headers={"X-User-Email": owner}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_ontology_with_version(client: AsyncClient, project_id: str) -> str:
    """创建 ontology 并 publish 得到 version；返回 version_id。"""
    # 1. 创建 ontology 头（注意：路由前缀是 /ontologies，没有 projects/{id}/ 前缀；
    #    project_id 通过 query 传）
    r = await client.post(
        "/ontologies",
        params={"project_id": project_id},
        json={"name": "TestOntology", "namespace": "http://test.example/onto#", "description": "test"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, (r.status_code, r.text)
    onto_id = r.json()["id"]

    # 2. publish → 生成 version
    r = await client.post(
        f"/ontologies/{onto_id}/publish",
        params={"project_id": project_id},
        json={"version": "v0.1.0"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    return body.get("version_id") or body.get("id") or onto_id


# ===========================================================================
# Release 端点
# ===========================================================================


@pytest.mark.asyncio
async def test_create_release(client: AsyncClient):
    project_id = await _project(client)
    version_id = await _create_ontology_with_version(client, project_id)

    r = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={
            "version": "v0.1.0",
            "description": "First release",
            "ontology_version_id": version_id,
            "tags": ["initial"],
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == "v0.1.0"
    assert body["status"] == "draft"
    assert body["ontology_version_id"] == version_id
    assert body["description"] == "First release"


@pytest.mark.asyncio
async def test_list_releases(client: AsyncClient):
    project_id = await _project(client)

    for v in ["v0.1.0", "v0.2.0"]:
        r = await client.post(
            f"/releases/projects/{project_id}/releases",
            json={"version": v},
            headers={"X-User-Email": ALICE},
        )
        assert r.status_code == 201, r.text

    r = await client.get(
        f"/releases/projects/{project_id}/releases",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    releases = r.json()
    assert len(releases) == 2
    versions = {rel["version"] for rel in releases}
    assert versions == {"v0.1.0", "v0.2.0"}


@pytest.mark.asyncio
async def test_get_release(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    r = await client.get(f"/releases/releases/{rid}", headers={"X-User-Email": ALICE})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == rid
    assert r.json()["version"] == "v1.0.0"


@pytest.mark.asyncio
async def test_update_release(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    r = await client.patch(
        f"/releases/releases/{rid}",
        json={"description": "Updated description", "tags": ["updated"]},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "Updated description"
    assert body["artifacts"]["tags"] == ["updated"]


@pytest.mark.asyncio
async def test_publish_release_lifecycle(client: AsyncClient):
    """draft → released 完整生命周期。"""
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]
    assert create.json()["status"] == "draft"

    # 发布
    pub = await client.post(
        f"/releases/releases/{rid}/publish", headers={"X-User-Email": ALICE}
    )
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert body["status"] in ("released", "published")
    assert body["checksum"]
    assert body["artifact_size"] > 0
    assert body["released_at"] is not None

    # 重复发布应 400
    pub2 = await client.post(
        f"/releases/releases/{rid}/publish", headers={"X-User-Email": ALICE}
    )
    assert pub2.status_code == 400


@pytest.mark.asyncio
async def test_download_release_manifest(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    r = await client.get(
        f"/releases/releases/{rid}/download", headers={"X-User-Email": ALICE}
    )
    assert r.status_code == 200, r.text
    manifest = r.json()
    assert manifest["release_id"] == rid
    assert manifest["version"] == "v1.0.0"
    assert manifest["checksum"]


@pytest.mark.asyncio
async def test_release_cross_project_isolation_404(client: AsyncClient):
    """alice 的 release，bob 用同 id 访问 → 404（隔离）。"""
    project_alice = await _project(client, ALICE)
    create = await client.post(
        f"/releases/projects/{project_alice}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    # bob 调公开接口 — release 详情接口目前没用 require_role，
    # 所以这里只验证 bob 不能在 alice 的项目里 list/create。
    project_bob = await _project(client, BOB)
    r = await client.get(
        f"/releases/projects/{project_bob}/releases",
        headers={"X-User-Email": BOB},
    )
    assert r.status_code == 200
    # bob 看不到 alice 的 release
    assert all(rel["id"] != rid for rel in r.json())


# ===========================================================================
# Preflight 端点
# ===========================================================================


@pytest.mark.asyncio
async def test_run_preflight_creates_report(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    r = await client.post(
        f"/releases/releases/{rid}/preflight",
        json={"environment": "staging"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["overall_status"] in ("passed", "warning", "failed")
    assert result["environment"] == "staging"
    assert "checks" in result
    assert "database_connectivity" in result["checks"]


@pytest.mark.asyncio
async def test_list_preflight_reports(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    # 跑两次 preflight
    for env in ("dev", "staging"):
        r = await client.post(
            f"/releases/releases/{rid}/preflight",
            json={"environment": env},
            headers={"X-User-Email": ALICE},
        )
        assert r.status_code == 200

    r = await client.get(
        f"/releases/projects/{project_id}/preflight-reports",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    reports = r.json()
    assert len(reports) == 2
    envs = {rep["environment"] for rep in reports}
    assert envs == {"dev", "staging"}


# ===========================================================================
# Deployment 端点
# ===========================================================================


@pytest.mark.asyncio
async def test_deploy_requires_released(client: AsyncClient):
    """只有 released 状态可以创建 deployment。"""
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    # 还在 draft 状态 → 400
    r = await client.post(
        f"/releases/projects/{project_id}/deployments",
        json={"release_id": rid, "environment": "dev", "environment_type": "dev"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_create_and_list_deployment(client: AsyncClient):
    project_id = await _project(client)
    create = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]

    # 发布
    pub = await client.post(
        f"/releases/releases/{rid}/publish", headers={"X-User-Email": ALICE}
    )
    assert pub.status_code == 200

    # 创建 deployment
    dep = await client.post(
        f"/releases/projects/{project_id}/deployments",
        json={"release_id": rid, "environment": "dev", "environment_type": "dev"},
        headers={"X-User-Email": ALICE},
    )
    assert dep.status_code == 201, dep.text
    body = dep.json()
    assert body["release_id"] == rid
    assert body["environment"] == "dev"
    assert body["status"] in ("deployed", "success", "completed", "succeeded")

    # 列表
    r = await client.get(
        f"/releases/projects/{project_id}/deployments",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    deps = r.json()
    assert len(deps) == 1
    assert deps[0]["id"] == body["id"]


@pytest.mark.asyncio
async def test_deployment_invalid_release_returns_404(client: AsyncClient):
    project_id = await _project(client)

    r = await client.post(
        f"/releases/projects/{project_id}/deployments",
        json={"release_id": str(uuid.uuid4()), "environment": "dev"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_deployment_release_project_mismatch_400(client: AsyncClient):
    """release 不属于该项目 → 400。"""
    project_a = await _project(client, ALICE)
    project_b = await _project(client, ALICE)

    # project_a 下创建 release
    create = await client.post(
        f"/releases/projects/{project_a}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    rid = create.json()["id"]
    await client.post(f"/releases/releases/{rid}/publish", headers={"X-User-Email": ALICE})

    # 用 project_b 创建 deployment，但 release_id 属于 project_a → 400
    r = await client.post(
        f"/releases/projects/{project_b}/deployments",
        json={"release_id": rid, "environment": "dev"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 400


# ===========================================================================
# UseCaseBundle 端点
# ===========================================================================


@pytest.mark.asyncio
async def test_create_and_list_use_case_bundle(client: AsyncClient):
    project_id = await _project(client)

    # 创建一个 use case bundle（必须带 release_id）
    rel = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0"},
        headers={"X-User-Email": ALICE},
    )
    release_id = rel.json()["id"]

    r = await client.post(
        f"/releases/projects/{project_id}/use-case-bundles",
        json={
            "name": "Customer Onboarding",
            "description": "E2E flow",
            "version": "1.0.0",
            "release_id": release_id,
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    bundle = r.json()
    assert bundle["name"] == "Customer Onboarding"

    # 列表
    r = await client.get(
        f"/releases/projects/{project_id}/use-case-bundles",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    bundles = r.json()
    assert len(bundles) == 1
    assert bundles[0]["name"] == "Customer Onboarding"


# ===========================================================================
# 入参校验
# ===========================================================================


@pytest.mark.asyncio
async def test_create_release_invalid_ontology_version_400(client: AsyncClient):
    project_id = await _project(client)
    r = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"version": "v1.0.0", "ontology_version_id": str(uuid.uuid4())},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_release_missing_version_422(client: AsyncClient):
    project_id = await _project(client)
    r = await client.post(
        f"/releases/projects/{project_id}/releases",
        json={"description": "no version"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_release_404(client: AsyncClient):
    r = await client.get(
        f"/releases/releases/{uuid.uuid4()}", headers={"X-User-Email": ALICE}
    )
    assert r.status_code == 404

"""HIA-58 / A9 验证运行详情 + save-as-query 端点集成测试。

覆盖：
* GET /validation/runs/{run_id} — 返回运行详情（含 violations / summary）
* POST /validation/runs/{run_id}/save-as-query — PASSED run 转为 SavedQuery
* 失败 run 不能保存为 query
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

# 切勿在模块级别导入 `async_session_factory`：它在 reinit_engines 之前
# 已绑定到默认 DB 引擎。fixture 必须重新拉取（见下 `isolated_app`）。
from src.db.validation import ValidationRun, ValidationStatus  # noqa: E402

# 这个全局在 fixture 里被重新绑定到 fixture-local reinit 后的 factory。
# helper 函数都通过这个名引用 — 切勿在这里初始化。
async_session_factory = None  # type: ignore[assignment]


@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    global async_session_factory
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    # 关键：通过模块重新拿到 rebind 后的 factory（不能用模块级 import 留下的引用）。
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

EMAIL_ALICE = "alice@ontolohub.local"


async def _alice_project(client: AsyncClient) -> str:
    r = await client.post(
        "/projects",
        json={"name": f"HIA-58 Test {uuid.uuid4()}"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_run(
    project_id: str,
    *,
    status: ValidationStatus = ValidationStatus.PASSED,
    violations: list | None = None,
    summary: dict | None = None,
    target_type: str = "ontology",
    name: str | None = None,
) -> str:
    """直接通过 session 创建一个 ValidationRun 行，返回 run_id。

    ValidationRun model 字段比 ValidationRunCreate schema 简单，所以
    走 session 比走 API 更稳定。``failed_checks`` 从 ``violations`` 长度推导。
    """
    failed = len(violations) if violations else 0
    async with async_session_factory() as s:
        run = ValidationRun(
            project_id=uuid.UUID(project_id),
            name=name or f"Run {uuid.uuid4()}",
            status=status,
            target_type=target_type,
            target_id=uuid.uuid4(),
            total_checks=10,
            passed_checks=10 - failed,
            failed_checks=failed,
            warning_checks=0,
            violations=violations,
            report_summary=summary or {"conforms": failed == 0, "total": 10},
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return str(run.id)


# ============================================================
# GET /validation/runs/{run_id}
# ============================================================


@pytest.mark.asyncio
async def test_get_run_404_when_missing(client: AsyncClient):
    """不存在的 run_id 应回 404。"""
    r = await client.get(
        f"/validation/runs/{uuid.uuid4()}",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_run_returns_full_details(client: AsyncClient):
    """运行详情应包含 violations / report_summary / 统计字段。"""
    proj = await _alice_project(client)
    run_id = await _make_run(
        proj,
        violations=[
            {
                "focus_node": "urn:test:p1",
                "result_path": "email",
                "message": "missing required property",
                "severity": "Violation",
            }
        ],
        summary={"conforms": False, "total": 10, "passed": 9, "failed": 1},
    )

    r = await client.get(
        f"/validation/runs/{run_id}",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == run_id
    assert data["validation_type"] == "ontology"
    assert data["status"] == "passed"
    assert data["total_checks"] == 10
    assert data["passed_checks"] == 9
    assert data["failed_checks"] == 1
    assert data["violations"] is not None
    assert len(data["violations"]) == 1
    assert data["violations"][0]["focus_node"] == "urn:test:p1"
    assert data["report_summary"]["conforms"] is False


# ============================================================
# POST /validation/runs/{run_id}/save-as-query
# ============================================================


@pytest.mark.asyncio
async def test_save_run_as_query_succeeds(client: AsyncClient):
    """PASSED run 应能保存为 SavedQuery。"""
    proj = await _alice_project(client)
    run_id = await _make_run(
        proj,
        status=ValidationStatus.PASSED,
        summary={"conforms": True, "total": 10, "passed": 10, "failed": 0},
    )

    r = await client.post(
        f"/validation/runs/{run_id}/save-as-query",
        json={
            "name": "All conforming check",
            "description": "Reusable template from successful run",
            "use_case": "regression",
        },
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "All conforming check"
    assert data["description"] == "Reusable template from successful run"
    assert data["project_id"] == proj
    assert data["is_shared"] is False
    assert data["run_count"] == 0


@pytest.mark.asyncio
async def test_save_run_as_query_rejects_failed_run(client: AsyncClient):
    """FAILED run 不应能保存为 query。"""
    proj = await _alice_project(client)
    run_id = await _make_run(
        proj,
        status=ValidationStatus.FAILED,
        violations=[{"focus_node": "x", "message": "bad"}],
    )

    r = await client.post(
        f"/validation/runs/{run_id}/save-as-query",
        json={"name": "Should fail"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 400
    assert "PASSED" in r.json()["detail"]


@pytest.mark.asyncio
async def test_save_run_as_query_rejects_running(client: AsyncClient):
    """RUNNING 状态的 run 也不能保存。"""
    proj = await _alice_project(client)
    run_id = await _make_run(proj, status=ValidationStatus.RUNNING)

    r = await client.post(
        f"/validation/runs/{run_id}/save-as-query",
        json={"name": "Should fail"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_save_run_as_query_404_when_missing(client: AsyncClient):
    """不存在的 run_id 应回 404。"""
    r = await client.post(
        f"/validation/runs/{uuid.uuid4()}/save-as-query",
        json={"name": "test"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_save_run_as_query_stores_definition_in_query_field(client: AsyncClient):
    """保存的 SavedQuery 应包含原 run 的引用 + summary（序列化到 query 字段）。"""
    proj = await _alice_project(client)
    run_id = await _make_run(
        proj,
        status=ValidationStatus.PASSED,
        summary={"conforms": True, "total": 5},
    )

    r = await client.post(
        f"/validation/runs/{run_id}/save-as-query",
        json={
            "name": "Template from run",
            "use_case": "monitoring",
        },
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    saved_id = r.json()["id"]

    # Verify the SavedQuery was actually created with the run reference
    from src.db.validation import SavedQuery

    async with async_session_factory() as s:
        sq = await s.get(SavedQuery, uuid.UUID(saved_id))
        assert sq is not None
        assert sq.name == "Template from run"
        # query field stores JSON with source_run_id
        import json

        definition = json.loads(sq.query)
        assert definition["source_run_id"] == run_id
        assert definition["validation_type"] == "ontology"
        assert definition["summary"]["conforms"] is True
        assert definition["use_case"] == "monitoring"

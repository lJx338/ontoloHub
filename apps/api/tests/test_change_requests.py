"""HIA-69 / B5 CR 工作流增强集成测试。

覆盖：
* CR 创建 / 提交 / 单 reviewer approve（自动 APPROVED）
* 多 reviewer 配置：两 reviewer 各自 approve 才升级
* 任一 reviewer reject → CR 进入 CHANGES_REQUESTED
* 评论线程：顶级 + reply
* 关闭 / 重新提交循环
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

from src.db.release import ChangeRequest  # noqa: E402  测试需要直接更新 CR 行


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

EMAIL_ALICE = "alice@example.com"
EMAIL_BOB = "bob@example.com"


async def _alice_project(client: AsyncClient) -> str:
    headers = {"X-User-Email": EMAIL_ALICE}
    r = await client.post("/projects", json={"name": "Alice P"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_cr(
    client: AsyncClient,
    project_id: str,
    *,
    required_approvers: int = 1,
    reviewer_ids: list[str] | None = None,
) -> dict:
    body = {
        "project_id": project_id,
        "title": "Test CR",
        "description": "demo",
        "changes": {"ontology_version": "v2"},
        "required_approvers": required_approvers,
    }
    if reviewer_ids:
        body["reviewer_ids"] = reviewer_ids
    r = await client.post(
        "/change-requests",
        json=body,
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _submit_cr(client: AsyncClient, cr_id: str) -> dict:
    r = await client.post(
        f"/change-requests/{cr_id}/submit",
        json={"submitted_by": str(uuid.uuid4()), "baseline_version": "v1"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_cr_create_starts_as_draft(client: AsyncClient):
    """默认创建 → DRAFT 状态。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)
    assert cr["status"] == "draft"
    assert cr["required_approvers"] == 1
    assert cr["changes"] == {"ontology_version": "v2"}


@pytest.mark.asyncio
async def test_cr_full_happy_path_single_reviewer(client: AsyncClient):
    """DRAFT → SUBMITTED → APPROVED → MERGED 全链路。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    submitted = await _submit_cr(client, cr["id"])
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"]
    assert submitted["baseline_version"] == "v1"

    # Approve
    r = await client.post(
        f"/change-requests/{cr['id']}/approve",
        json={"approved_by": str(uuid.uuid4())},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"
    assert approved["approved_at"]

    # Merge
    r = await client.post(
        f"/change-requests/{cr['id']}/merge",
        json={"merged_by": str(uuid.uuid4()), "target_version": "v2"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["status"] == "merged"
    assert merged["target_version"] == "v2"
    assert merged["merged_at"]


@pytest.mark.asyncio
async def test_cr_cannot_approve_when_not_submitted(client: AsyncClient):
    """DRAFT 不能直接 approve。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)
    r = await client.post(
        f"/change-requests/{cr['id']}/approve",
        json={"approved_by": str(uuid.uuid4())},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_cr_cannot_modify_after_merge(client: AsyncClient):
    """MERGED 之后 PATCH 必须 400。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)
    await _submit_cr(client, cr["id"])
    await client.post(
        f"/change-requests/{cr['id']}/approve",
        json={},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    await client.post(
        f"/change-requests/{cr['id']}/merge",
        json={},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    r = await client.patch(
        f"/change-requests/{cr['id']}",
        json={"title": "should fail"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 400


# ============================================================
# HIA-69 / B5 多 reviewer + 自动合并
# ============================================================


@pytest.mark.asyncio
async def test_multi_reviewer_auto_merge_after_two_approves(client: AsyncClient):
    """B5 验收点：2 reviewers 各自 approve 才升 APPROVED。

    1. 创建 CR with required_approvers=2 + 两个 reviewer
    2. 提交 → SUBMITTED
    3. reviewer1 approve → 还应该 SUBMITTED（只 1 个）
    4. reviewer2 approve → 自动 APPROVED
    """
    proj = await _alice_project(client)
    reviewer_a = str(uuid.uuid4())
    reviewer_b = str(uuid.uuid4())
    cr = await _create_cr(
        client,
        proj,
        required_approvers=2,
        reviewer_ids=[reviewer_a, reviewer_b],
    )

    assert cr["status"] == "draft"

    # 列出 reviewers
    r = await client.get(
        f"/change-requests/{cr['id']}/reviewers",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200
    reviewers = r.json()
    assert len(reviewers) == 2
    assert all(rv["status"] == "pending" for rv in reviewers)

    await _submit_cr(client, cr["id"])

    # reviewer_a approve → still SUBMITTED
    r = await client.post(
        f"/change-requests/{cr['id']}/reviewers/{reviewer_a}/approve",
        json={"comment": "lgtm"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    # reviewer_b approve → auto APPROVED
    r = await client.post(
        f"/change-requests/{cr['id']}/reviewers/{reviewer_b}/approve",
        json={"comment": "ship it"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == reviewer_b
    assert body["approved_at"]


@pytest.mark.asyncio
async def test_reviewer_changes_requested_flips_cr_status(client: AsyncClient):
    """任一 reviewer 请求修改 → CR 进入 CHANGES_REQUESTED。"""
    proj = await _alice_project(client)
    reviewer_a = str(uuid.uuid4())
    reviewer_b = str(uuid.uuid4())
    cr = await _create_cr(
        client,
        proj,
        required_approvers=2,
        reviewer_ids=[reviewer_a, reviewer_b],
    )
    await _submit_cr(client, cr["id"])

    r = await client.post(
        f"/change-requests/{cr['id']}/reviewers/{reviewer_a}/reject",
        json={"comment": "please fix"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "changes_requested"


@pytest.mark.asyncio
async def test_resubmit_resets_reviewer_status(client: AsyncClient):
    """从 CHANGES_REQUESTED 重新提交 → 所有 reviewer 状态重置为 PENDING。"""
    proj = await _alice_project(client)
    reviewer_a = str(uuid.uuid4())
    reviewer_b = str(uuid.uuid4())
    cr = await _create_cr(
        client,
        proj,
        required_approvers=2,
        reviewer_ids=[reviewer_a, reviewer_b],
    )
    await _submit_cr(client, cr["id"])

    # reviewer_a requests changes
    await client.post(
        f"/change-requests/{cr['id']}/reviewers/{reviewer_a}/reject",
        json={"comment": "fix"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert (
        await client.get(
            f"/change-requests/{cr['id']}",
            headers={"X-User-Email": EMAIL_ALICE},
        )
    ).json()["status"] == "changes_requested"

    # 重新提交 — 应该允许从 CHANGES_REQUESTED
    r = await client.post(
        f"/change-requests/{cr['id']}/submit",
        json={"baseline_version": "v2"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    # reviewers should be back to pending
    reviewers = (
        await client.get(
            f"/change-requests/{cr['id']}/reviewers",
            headers={"X-User-Email": EMAIL_ALICE},
        )
    ).json()
    assert all(rv["status"] == "pending" for rv in reviewers)


@pytest.mark.asyncio
async def test_assign_reviewer_idempotent(client: AsyncClient):
    """同一 reviewer 重复分配 → 409。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)
    reviewer_id = str(uuid.uuid4())

    r1 = await client.post(
        f"/change-requests/{cr['id']}/reviewers",
        json={"reviewer_id": reviewer_id, "reviewer_name": "Alice"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r1.status_code == 201

    r2 = await client.post(
        f"/change-requests/{cr['id']}/reviewers",
        json={"reviewer_id": reviewer_id},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_required_approvers_zero_auto_approves(client: AsyncClient):
    """required_approvers=0 + submit → 自动 APPROVED（无需 reviewer）。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj, required_approvers=0)

    r = await client.post(
        f"/change-requests/{cr['id']}/submit",
        json={},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    # submit endpoint doesn't auto-approve; user must call /approve
    assert r.json()["status"] == "submitted"

    r = await client.post(
        f"/change-requests/{cr['id']}/approve",
        json={},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_close_cr(client: AsyncClient):
    """任意非终态 → CLOSED。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    r = await client.post(
        f"/change-requests/{cr['id']}/close",
        json={"close_reason": "abandoned"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed"
    assert body["close_reason"] == "abandoned"
    assert body["closed_at"]


# ============================================================
# 评论线程
# ============================================================


@pytest.mark.asyncio
async def test_comment_thread_top_level_then_reply(client: AsyncClient):
    """顶级评论 + reply（parent_id 指向顶级）。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    # top-level
    r = await client.post(
        f"/change-requests/{cr['id']}/comments",
        json={
            "body": "please add field X",
            "author_id": str(uuid.uuid4()),
            "author_name": "alice",
        },
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    top = r.json()
    assert top["parent_id"] is None
    assert top["body"] == "please add field X"

    # reply
    r = await client.post(
        f"/change-requests/{cr['id']}/comments",
        json={
            "body": "will do in v3",
            "author_id": str(uuid.uuid4()),
            "author_name": "bob",
            "parent_id": top["id"],
        },
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    reply = r.json()
    assert reply["parent_id"] == top["id"]

    # list
    r = await client.get(
        f"/change-requests/{cr['id']}/comments",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200
    comments = r.json()
    assert len(comments) == 2
    assert comments[0]["id"] == top["id"]
    assert comments[1]["id"] == reply["id"]


@pytest.mark.asyncio
async def test_reply_parent_must_be_same_cr(client: AsyncClient):
    """reply 的 parent_id 跨 CR 不接受。"""
    proj = await _alice_project(client)
    cr_a = await _create_cr(client, proj)
    cr_b = await _create_cr(client, proj, required_approvers=2)

    # comment on A
    r = await client.post(
        f"/change-requests/{cr_a['id']}/comments",
        json={"body": "on A"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    parent_a = r.json()["id"]

    # try reply on B pointing to A's comment — should 400
    r = await client.post(
        f"/change-requests/{cr_b['id']}/comments",
        json={"body": "should fail", "parent_id": parent_a},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_comment_soft_deletes(client: AsyncClient):
    """删除评论 → body 替换为 '[deleted]'，结构保留。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    r = await client.post(
        f"/change-requests/{cr['id']}/comments",
        json={"body": "oops"},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    cid = r.json()["id"]

    r = await client.delete(
        f"/change-requests/{cr['id']}/comments/{cid}",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 204

    listed = (
        await client.get(
            f"/change-requests/{cr['id']}/comments",
            headers={"X-User-Email": EMAIL_ALICE},
        )
    ).json()
    assert len(listed) == 1
    assert listed[0]["body"] == "[deleted]"
    assert listed[0]["deleted_at"]


# ============================================================
# 列表 / 筛选
# ============================================================


@pytest.mark.asyncio
async def test_list_cr_filter_by_project(client: AsyncClient):
    """按 project_id 筛选 CR。"""
    proj_a = await _alice_project(client)
    proj_b = await _alice_project(client)

    cr_a = await _create_cr(client, proj_a)
    cr_b = await _create_cr(client, proj_b)

    r = await client.get(
        f"/change-requests?project_id={proj_a}",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200
    listed = r.json()
    ids = {c["id"] for c in listed}
    assert cr_a["id"] in ids
    assert cr_b["id"] not in ids


@pytest.mark.asyncio
async def test_list_cr_filter_by_status(client: AsyncClient):
    """按 status 筛选 CR。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)
    await _submit_cr(client, cr["id"])

    r = await client.get(
        "/change-requests?status=submitted",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200
    listed = r.json()
    assert any(c["id"] == cr["id"] for c in listed)
    assert all(c["status"] == "submitted" for c in listed)


@pytest.mark.asyncio
async def test_diff_helper_directly_unit():
    """_diff_snapshots helper: 直接单元测试，不走 HTTP。

    HIA-57: 验证简化版快照对比逻辑，覆盖 added / removed / modified / unchanged 四种。
    """
    from src.api.release import _diff_snapshots, ChangeRequestDiffEntry

    from_snap = {
        "class_snapshot": [
            {"iri": "urn:P", "name": "Person"},
            {"iri": "urn:O", "name": "Order"},
        ],
        "property_snapshot": [
            {"iri": "urn:p1", "name": "id", "domain_iri": "urn:P"},
        ],
        "relation_snapshot": [],
    }
    to_snap = {
        "class_snapshot": [
            {"iri": "urn:P", "name": "PersonRenamed"},  # modified
            # urn:O removed
            {"iri": "urn:C", "name": "Company"},  # added
        ],
        "property_snapshot": [
            {"iri": "urn:p1", "name": "id", "domain_iri": "urn:P"},  # unchanged
            {"iri": "urn:p2", "name": "email", "domain_iri": "urn:P"},  # added
        ],
        "relation_snapshot": [],
    }

    diffs, summary = _diff_snapshots(from_snap, to_snap)

    assert summary == {
        "added": 2,  # Company + email
        "removed": 1,  # Order
        "modified": 1,  # Person name change
        "unchanged": 1,  # id property
    }
    # Person is modified
    person_modified = [
        e
        for e in diffs
        if e.kind == "class" and e.iri == "urn:P" and e.change == "modified"
    ]
    assert len(person_modified) == 1
    # Person should have details with before/after
    assert "name" in person_modified[0].details["after"]
    assert person_modified[0].details["after"]["name"] == "PersonRenamed"


# ============================================================
# Diff 端点（HIA-57 / A10 收尾补全）
# ============================================================


# ============================================================
# Diff 端点（HIA-57 / A10 收尾补全）
# ============================================================


async def _make_ontology(client: AsyncClient, project_id: str) -> str:
    """创建一个 ontology，返回 ontology_id。"""
    r = await client.post(
        "/ontologies",
        json={
            "name": f"Diff Test Ontology {uuid.uuid4()}",
            "namespace": f"urn:test:{uuid.uuid4()}",
            "project_id": project_id,
        },
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_ontology_version_with_snapshots(
    client: AsyncClient,
    ontology_id: str,
    *,
    version: str,
    class_snapshot: list[dict],
    property_snapshot: list[dict],
    relation_snapshot: list[dict] | None = None,
) -> str:
    """创建 ontology version 并直接通过 session 写入快照内容。

    走 API（``PUT /versions/{id}/content``）也行，但要求外层 Pydantic
    schema 完全对齐；测试里直接 SQLAlchemy 写更稳定。
    """
    create_r = await client.post(
        f"/ontologies/{ontology_id}/versions",
        json={"version": version},
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert create_r.status_code == 201, create_r.text
    version_id = create_r.json()["id"]

    from src.db import connection as conn
    from src.db.ontology import OntologyVersion

    async with conn.async_session_factory() as s:
        v = await s.get(OntologyVersion, uuid.UUID(version_id))
        assert v is not None
        v.class_snapshot = class_snapshot
        v.property_snapshot = property_snapshot
        v.relation_snapshot = relation_snapshot or []
        await s.commit()

    return version_id


async def _set_cr_versions(
    session_factory,
    cr_id: str,
    baseline_version_id: str | None = None,
    target_version_id: str | None = None,
    stored_diff: dict | None = None,
) -> None:
    """直接通过 session 更新 CR 的 baseline/target/diff 字段。

    公开 API 不支持写 ``baseline_version_id`` / ``target_version_id`` 在
    同一调用里同时设置，也不支持写 ``diff`` 字段 — 测试需要这些场景。

    **必须 ``commit()``**：HTTP 请求走单独的 session，flush 仅在事务内可
    见；不 commit 后续 GET 会读不到。
    """
    async with session_factory() as s:
        cr = await s.get(ChangeRequest, uuid.UUID(cr_id))
        assert cr is not None
        if baseline_version_id:
            cr.baseline_version_id = uuid.UUID(baseline_version_id)
            cr.baseline_version = "v1"
        if target_version_id:
            cr.target_version_id = uuid.UUID(target_version_id)
            cr.target_version = "v2"
        if stored_diff is not None:
            cr.diff = stored_diff
        await s.commit()


@pytest.mark.asyncio
async def test_cr_diff_404_for_nonexistent_cr(client: AsyncClient):
    """不存在的 CR 应回 404。"""
    r = await client.get(
        f"/change-requests/{uuid.uuid4()}/diff",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cr_diff_422_without_versions(client: AsyncClient):
    """CR 没有 baseline/target 版本时回 422。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    r = await client.get(
        f"/change-requests/{cr['id']}/diff",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 422
    assert "baseline_version_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_cr_diff_returns_stored_diff(client: AsyncClient, isolated_app):
    """CR.diff 已写入时直接返回（computed_from=stored）。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    stored = {
        "entries": [
            {
                "kind": "class",
                "iri": "urn:test:Person",
                "name": "Person",
                "change": "added",
            },
            {
                "kind": "property",
                "iri": "urn:test:email",
                "name": "email",
                "change": "modified",
            },
        ],
        "summary": {"added": 1, "removed": 0, "modified": 1, "unchanged": 0},
    }

    _, app = isolated_app
    from src.db import connection as conn

    await _set_cr_versions(
        conn.async_session_factory,
        cr["id"],
        stored_diff=stored,
    )

    r = await client.get(
        f"/change-requests/{cr['id']}/diff",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["computed_from"] == "stored"
    assert data["summary"] == stored["summary"]
    assert len(data["diff"]) == 2
    assert data["diff"][0]["kind"] == "class"
    assert data["diff"][0]["change"] == "added"


@pytest.mark.asyncio
async def test_cr_diff_computes_from_snapshots(client: AsyncClient):
    """从 OntologyVersion 快照计算 diff（computed_from=snapshots）。"""
    proj = await _alice_project(client)
    onto_id = await _make_ontology(client, proj)

    # baseline: 1 class (Person), 1 property (id) — 用 iri 字段标识
    baseline = await _make_ontology_version_with_snapshots(
        client,
        onto_id,
        version="v1",
        class_snapshot=[
            {"iri": "urn:test:Person", "name": "Person"},
        ],
        property_snapshot=[
            {"iri": "urn:test:person_id", "name": "id", "domain_iri": "urn:test:Person"},
        ],
    )

    # target: 2 classes (Person, Company), 2 properties (id, email)
    target = await _make_ontology_version_with_snapshots(
        client,
        onto_id,
        version="v2",
        class_snapshot=[
            {"iri": "urn:test:Person", "name": "Person"},
            {"iri": "urn:test:Company", "name": "Company"},
        ],
        property_snapshot=[
            {"iri": "urn:test:person_id", "name": "id", "domain_iri": "urn:test:Person"},
            {"iri": "urn:test:person_email", "name": "email", "domain_iri": "urn:test:Person"},
        ],
    )

    cr = await _create_cr(client, proj)

    # Bind baseline + target to CR via session
    from src.db import connection as conn

    await _set_cr_versions(
        conn.async_session_factory,
        cr["id"],
        baseline_version_id=baseline,
        target_version_id=target,
    )

    r = await client.get(
        f"/change-requests/{cr['id']}/diff",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["computed_from"] == "snapshots"
    assert data["baseline_version_id"] == baseline
    assert data["target_version_id"] == target
    # Company class + email property should be 'added'
    assert data["summary"]["added"] == 2
    assert data["summary"]["unchanged"] >= 1  # Person class + person_id property
    # Diff entries should include the new Company class
    added_classes = [
        e for e in data["diff"] if e["kind"] == "class" and e["change"] == "added"
    ]
    added_names = {e["name"] for e in added_classes}
    assert "Company" in added_names


@pytest.mark.asyncio
async def test_cr_diff_404_when_version_record_missing(client: AsyncClient, isolated_app):
    """baseline_version_id 指向不存在的 OntologyVersion 时回 404。"""
    proj = await _alice_project(client)
    cr = await _create_cr(client, proj)

    from src.db import connection as conn

    # Set baseline_version_id to a valid UUID but the row doesn't exist
    fake_uuid = str(uuid.uuid4())
    async with conn.async_session_factory() as s:
        cr_obj = await s.get(ChangeRequest, uuid.UUID(cr["id"]))
        cr_obj.baseline_version_id = uuid.UUID(fake_uuid)
        cr_obj.target_version_id = uuid.UUID(str(uuid.uuid4()))
        await s.commit()

    r = await client.get(
        f"/change-requests/{cr['id']}/diff",
        headers={"X-User-Email": EMAIL_ALICE},
    )
    assert r.status_code == 404

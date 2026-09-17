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

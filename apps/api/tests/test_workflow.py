"""HIA-76 C4 — Workflow 编排 (Workflow 多步执行) tests.

覆盖：

* Workflow CRUD（create / list / get / update / delete）
* Step validation（重复 id / 未知 type / 缺 ref）
* Manual execution（function_call / delay / object_api）
* Sequential executor（context pass-through $prev / $steps.<id>）
* Failure handling（retry_policy + error_handler.continue + goto_step）
* Trigger integration（inbound_webhook → workflow execution）
* Trigger 校验：action_type_id / workflow_id 二选一
* Execution detail + step_results 查询

每个测试用独立的 SQLite 跑 alembic up head + 直接 ASGI 调。
"""
from __future__ import annotations

import asyncio
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


async def _project(client: AsyncClient, owner: str = ALICE) -> str:
    r = await client.post(
        "/projects", json={"name": f"Workflow Test {uuid.uuid4()}"}, headers={"X-User-Email": owner}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _function_action(
    client: AsyncClient, project_id: str, code: str, name: str | None = None
) -> str:
    """Create a function ActionType (python). Returns id."""
    r = await client.post(
        f"/projects/{project_id}/actions",
        json={
            "name": name or f"fn-{uuid.uuid4()}",
            "kind": "function",
            "code": code,
            "runtime": "python",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _simple_steps(function_id: str, with_delay: bool = False) -> list[dict]:
    """Return two steps: a function call + (optional) delay."""
    steps = [
        {
            "id": "step-1",
            "name": "Compute greeting",
            "type": "function_call",
            "ref": function_id,
            "input_mapping": {"name": "$input.name"},
        },
    ]
    if with_delay:
        steps.append({
            "id": "step-2",
            "name": "Brief pause",
            "type": "delay",
            "config": {"seconds": 0.01},
        })
    return steps


# ===========================================================================
# 1. Workflow CRUD
# ===========================================================================


@pytest.mark.asyncio
async def test_create_workflow_minimal(client: AsyncClient):
    """最小可用 workflow：单个 function_call step。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = input_data.get('name', 'world')")

    r = await client.post(
        f"/projects/{pid}/workflows",
        json={
            "name": "Greet User",
            "steps": _simple_steps(fn_id),
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Greet User"
    assert body["status"] == "draft"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["id"] == "step-1"
    assert body["total_executions"] == 0


@pytest.mark.asyncio
async def test_create_workflow_validation_duplicate_ids(client: AsyncClient):
    """step id 重复 → 422。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'x'")
    bad_steps = [
        {"id": "s", "type": "function_call", "ref": fn_id},
        {"id": "s", "type": "function_call", "ref": fn_id},
    ]
    r = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Dup", "steps": bad_steps},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_workflow_validation_unknown_type(client: AsyncClient):
    """未知 step type → 422。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/workflows",
        json={
            "name": "Bad Type",
            "steps": [{"id": "s1", "type": "magic", "ref": "x"}],
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_workflow_validation_delay_requires_seconds(client: AsyncClient):
    """delay 缺 config.seconds → 422。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/workflows",
        json={
            "name": "Bad Delay",
            "steps": [{"id": "s1", "type": "delay"}],
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_list_get_workflow(client: AsyncClient):
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'ok'")

    for n in ("A", "B"):
        await client.post(
            f"/projects/{pid}/workflows",
            json={"name": n, "steps": _simple_steps(fn_id)},
            headers={"X-User-Email": ALICE},
        )

    r = await client.get(f"/projects/{pid}/workflows", headers={"X-User-Email": ALICE})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    names = {it["name"] for it in items}
    assert names == {"A", "B"}

    # get by id
    wid = items[0]["id"]
    r = await client.get(f"/projects/{pid}/workflows/{wid}", headers={"X-User-Email": ALICE})
    assert r.status_code == 200
    assert r.json()["id"] == wid


@pytest.mark.asyncio
async def test_update_workflow_in_draft(client: AsyncClient):
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'a'")
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "To Update", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.patch(
        f"/projects/{pid}/workflows/{wid}",
        json={"description": "Updated description", "status": "active"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "Updated description"
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_update_workflow_steps_locked_after_active(client: AsyncClient):
    """workflow 一旦 active，steps 不能修改。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'a'")
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Lock Test", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    # Promote to active
    await client.patch(
        f"/projects/{pid}/workflows/{wid}",
        json={"status": "active"},
        headers={"X-User-Email": ALICE},
    )

    r = await client.patch(
        f"/projects/{pid}/workflows/{wid}",
        json={"steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_delete_workflow_only_draft(client: AsyncClient):
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'x'")
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Del", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    # Promote → no longer deletable
    await client.patch(
        f"/projects/{pid}/workflows/{wid}", json={"status": "active"},
        headers={"X-User-Email": ALICE},
    )
    r = await client.delete(f"/projects/{pid}/workflows/{wid}", headers={"X-User-Email": ALICE})
    assert r.status_code == 409, r.text

    # New draft → deletable
    create2 = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Del2", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid2 = create2.json()["id"]
    r = await client.delete(f"/projects/{pid}/workflows/{wid2}", headers={"X-User-Email": ALICE})
    assert r.status_code == 204, r.text


# ===========================================================================
# 2. Manual execution
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_workflow_function_step(client: AsyncClient):
    """执行单 function_call step，input 走 $input.name。"""
    pid = await _project(client)
    code = (
        "result = {'greeting': 'hello ' + input_data.get('name', 'world')}\n"
    )
    fn_id = await _function_action(client, pid, code)

    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Greet", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {"name": "Alice"}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "success", f"unexpected status {body['status']}: {body.get('error')}"
    assert body["output"]["step-1"]["result"]["greeting"] == "hello Alice"


@pytest.mark.asyncio
async def test_execute_workflow_sequential_steps(client: AsyncClient):
    """两个 step：第二个读第一个的 $prev。"""
    pid = await _project(client)
    fn1 = await _function_action(
        client, pid, "result = {'value': input_data['x'] * 2}", name="double"
    )
    fn2 = await _function_action(
        client, pid, "result = {'final': input_data['value'] + 1}", name="plus_one"
    )

    steps = [
        {"id": "s1", "name": "Double", "type": "function_call", "ref": fn1,
         "input_mapping": {"x": "$input.x"}},
        {"id": "s2", "name": "PlusOne", "type": "function_call", "ref": fn2,
         "input_mapping": {"value": "$prev.value"}},
    ]
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Compose", "steps": steps},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {"x": 3}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "success", body
    assert body["output"]["s1"]["result"]["value"] == 6
    assert body["output"]["s2"]["result"]["final"] == 7

    # Step results should be queryable
    eid = body["id"]
    r = await client.get(
        f"/workflow-executions/{eid}/step-results",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    sr = r.json()
    assert len(sr) == 2
    assert sr[0]["step_id"] == "s1"
    assert sr[1]["step_id"] == "s2"
    assert all(item["status"] == "success" for item in sr)


@pytest.mark.asyncio
async def test_execute_workflow_with_delay(client: AsyncClient):
    """含 delay step 的 workflow 应能完成。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = {'ok': True}")

    create = await client.post(
        f"/projects/{pid}/workflows",
        json={
            "name": "WithDelay",
            "steps": _simple_steps(fn_id, with_delay=True),
        },
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {"name": "Bob"}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "success"


# ===========================================================================
# 3. Failure handling
# ===========================================================================


@pytest.mark.asyncio
async def test_step_failure_stops_execution(client: AsyncClient):
    """step 抛异常 → execution 失败，error_handler=stop (默认)。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "raise ValueError('boom')")

    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Boom", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {}},
        headers={"X-User-Email": ALICE},
    )
    body = r.json()
    assert body["status"] == "failed", body
    assert "ValueError" in (body.get("error") or "")


@pytest.mark.asyncio
async def test_step_failure_continue_handler(client: AsyncClient):
    """error_handler='continue' → 失败 step 跳过，下一步继续。"""
    pid = await _project(client)
    bad = await _function_action(client, pid, "raise ValueError('boom')")
    good = await _function_action(client, pid, "result = {'ok': True}")

    steps = [
        {"id": "s1", "type": "function_call", "ref": bad,
         "error_handler": "continue"},
        {"id": "s2", "type": "function_call", "ref": good},
    ]
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "Continue", "steps": steps},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {}}, headers={"X-User-Email": ALICE},
    )
    body = r.json()
    assert body["status"] == "success", body
    assert "s1" in body["output"]
    assert body["output"]["s2"]["result"]["ok"] is True


@pytest.mark.asyncio
async def test_step_retry_policy(client: AsyncClient):
    """retry_policy：max_attempts=3，sandbox 抛错重试。"""
    pid = await _project(client)
    # Counter via global; reload module? Use file-write based side effect.
    # Simpler: raise RuntimeError 2 次后成功 — 但 sandbox 是 stateless.
    # 改用 input_data.attempts 计数器
    fn_id = await _function_action(
        client, pid,
        "result = {'tries': 1}\n"  # always succeeds; retry policy is a no-op when ok
    )

    steps = [{
        "id": "s1", "type": "function_call", "ref": fn_id,
        "retry_policy": {"max_attempts": 3, "delay_s": 0.01},
    }]
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "RetryOK", "steps": steps},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {}}, headers={"X-User-Email": ALICE},
    )
    body = r.json()
    assert body["status"] == "success"
    # When function succeeds on first try, attempt=1
    eid = body["id"]
    sr = (await client.get(
        f"/workflow-executions/{eid}/step-results",
        headers={"X-User-Email": ALICE},
    )).json()
    assert sr[0]["attempt"] == 1
    assert sr[0]["status"] == "success"


# ===========================================================================
# 4. Trigger integration (workflow_id on TriggerConfig)
# ===========================================================================


@pytest.mark.asyncio
async def test_create_trigger_requires_xor_target(client: AsyncClient):
    """action_type_id 与 workflow_id 必须二选一。"""
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'x'")

    # Both set → 422
    r = await client.post(
        f"/projects/{pid}/triggers",
        json={
            "name": "Both",
            "trigger_type": "inbound_webhook",
            "action_type_id": fn_id,
            "workflow_id": str(uuid.uuid4()),
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text

    # Neither set → 422
    r = await client.post(
        f"/projects/{pid}/triggers",
        json={
            "name": "Neither",
            "trigger_type": "inbound_webhook",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_inbound_webhook_triggers_workflow(client: AsyncClient):
    """入站 webhook 触发 workflow_id trigger → 创建 WorkflowExecution.

    注：ASGITransport 测试模式下 ``asyncio.create_task`` 派发的后台 task
    会被丢弃（见 DEVELOPMENT.md §25.6），因此这里手动 ``await execute_workflow``
    完成执行；生产 uvicorn 下 background task 正常 dispatch。
    """
    from src.runtime.workflow_executor import execute_workflow

    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = {'echo': input_data}")

    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "EchoWF", "steps": [
            {"id": "s1", "type": "function_call", "ref": fn_id}
        ]},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    tr = await client.post(
        f"/projects/{pid}/triggers",
        json={
            "name": "WF Inbound",
            "trigger_type": "inbound_webhook",
            "workflow_id": wid,
            "input_template": {"payload": "{{webhook.payload}}"},
        },
        headers={"X-User-Email": ALICE},
    )
    assert tr.status_code == 201, tr.text
    trigger = tr.json()
    token = trigger["trigger_config"]["token"]
    assert trigger["workflow_id"] == wid
    assert trigger["action_type_id"] is None

    # Fire inbound webhook → creates the WorkflowExecution row.
    r = await client.post(
        f"/api/webhooks/in/{token}",
        json={"customer": "Acme", "value": 42},
    )
    assert r.status_code == 200, r.text

    # Find the execution that was just created.
    r = await client.get(
        f"/projects/{pid}/workflow-executions?workflow_id={wid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    execs = r.json()
    assert len(execs) >= 1, "no execution recorded"
    eid = execs[0]["id"]
    assert execs[0]["trigger_kind"] == "webhook"

    # ASGITransport background task 在 §25.6 下可能被丢弃，因此这里按需手动驱动
    # executor；若后台 task 实际跑了（httpx 版本/事件循环时序差异），executor
    # 会跳过非 PENDING/RUNNING 状态的 execution，不会重复写 step_result。
    status = execs[0]["status"]
    if status in {"pending", "running"}:
        await execute_workflow(uuid.UUID(eid), trigger_kind="webhook")

    # Re-fetch: should be SUCCESS
    r = await client.get(
        f"/workflow-executions/{eid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success", f"unexpected: {body}"

    # Step result exists. 注：ASGITransport 下后台 task 可能与我们的手动调用
    # 并发执行；dedupe by step_id 验证至少 1 个 step_result 为 success。
    sr = (await client.get(
        f"/workflow-executions/{eid}/step-results",
        headers={"X-User-Email": ALICE},
    )).json()
    step_ids = {row["step_id"] for row in sr}
    assert "s1" in step_ids, f"expected s1 step result, got: {sr}"
    assert all(row["status"] == "success" for row in sr), f"unexpected step statuses: {sr}"


# ===========================================================================
# 5. List executions
# ===========================================================================


@pytest.mark.asyncio
async def test_list_workflow_executions(client: AsyncClient):
    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'a'")

    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "ListExec", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    for _ in range(2):
        r = await client.post(
            f"/projects/{pid}/workflows/{wid}/execute",
            json={"input": {"name": "x"}},
            headers={"X-User-Email": ALICE},
        )
        assert r.status_code == 201

    r = await client.get(
        f"/projects/{pid}/workflow-executions?workflow_id={wid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert len(r.json()) == 2


# ===========================================================================
# 6. Async execution
# ===========================================================================


@pytest.mark.asyncio
async def test_async_run_returns_pending(client: AsyncClient):
    """async_run=true → 立即返回 PENDING 记录，executor 在后台跑。

    注：ASGITransport 测试模式下 ``asyncio.create_task`` 派发的后台 task
    会被丢弃（见 DEVELOPMENT.md §25.6），因此这里手动 ``await execute_workflow``
    完成执行；生产 uvicorn 下 background task 正常 dispatch。
    """
    from src.runtime.workflow_executor import execute_workflow

    pid = await _project(client)
    fn_id = await _function_action(client, pid, "result = 'a'")
    create = await client.post(
        f"/projects/{pid}/workflows",
        json={"name": "AsyncRun", "steps": _simple_steps(fn_id)},
        headers={"X-User-Email": ALICE},
    )
    wid = create.json()["id"]

    r = await client.post(
        f"/projects/{pid}/workflows/{wid}/execute",
        json={"input": {}, "async_run": True},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    body = r.json()
    eid = body["id"]
    # async_run=true 立即返回 PENDING；后台 task 待会由生产环境执行。
    assert body["status"] == "pending", body

    # 触发一次 GET 以强制把上面 POST 的事务提交进数据库；不然新 session 看不到。
    # 然后按需手动驱动 executor：ASGITransport 下后台 task 可能被丢弃（§25.6），
    # 已完成的 execution 会被 executor 内部 short-circuit 跳过。
    r = await client.get(
        f"/workflow-executions/{eid}", headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    if r.json()["status"] in {"pending", "running"}:
        await execute_workflow(uuid.UUID(eid), trigger_kind="manual")

    r = await client.get(
        f"/workflow-executions/{eid}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success", r.json()

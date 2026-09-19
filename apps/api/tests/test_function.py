"""HIA-70 C1 — Function CRUD + test-run endpoint tests.

覆盖：
* Function CRUD: create / list / get / update / delete
* api_name 唯一约束（409 on duplicate）
* version 在 source_code 变更时 bump
* ``/test`` 端点：Python 同步执行 + 返回 result
* ``/test`` 失败：错误透传到 ``error`` 字段
* ``/test`` 超时：timeout_s 控制
* ``/runs`` 列出最近试运行

每个测试用独立 SQLite + alembic up head。
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


# ---------- per-test DB override ----------


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
        "/projects", json={"name": f"Function Test {uuid.uuid4()}"},
        headers={"X-User-Email": owner},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_function(
    client: AsyncClient, pid: str, api_name: str = "compute_ltv",
    code: str = "result = input_data.get('x', 0) * 2",
) -> dict:
    r = await client.post(
        f"/projects/{pid}/functions",
        json={
            "api_name": api_name,
            "display_name": "Compute LTV",
            "description": "compute lifetime value",
            "language": "python",
            "source_code": code,
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# CRUD
# ===========================================================================


@pytest.mark.asyncio
async def test_create_function_minimal(client: AsyncClient):
    """最小 Function 创建：仅 api_name + display_name。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/functions",
        json={"api_name": "fn1", "display_name": "FN1"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["api_name"] == "fn1"
    assert body["display_name"] == "FN1"
    assert body["language"] == "python"
    assert body["version"] == 1
    assert body["source_code"] == ""


@pytest.mark.asyncio
async def test_create_function_duplicate_api_name_409(client: AsyncClient):
    """同项目内 api_name 重复 → 409。"""
    pid = await _project(client)
    await _make_function(client, pid, api_name="dup")
    r = await client.post(
        f"/projects/{pid}/functions",
        json={"api_name": "dup", "display_name": "Dup2"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_create_function_invalid_api_name_422(client: AsyncClient):
    """api_name 非法（数字开头）→ 422 (Pydantic)。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/functions",
        json={"api_name": "9bad", "display_name": "Bad"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_list_functions(client: AsyncClient):
    """列出项目 Function。"""
    pid = await _project(client)
    await _make_function(client, pid, api_name="a1")
    await _make_function(client, pid, api_name="b2")
    r = await client.get(
        f"/projects/{pid}/functions", headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    names = sorted(item["api_name"] for item in items)
    assert names == ["a1", "b2"]


@pytest.mark.asyncio
async def test_list_functions_filter_by_language(client: AsyncClient):
    """language 过滤。"""
    pid = await _project(client)
    await _make_function(client, pid, api_name="py1")
    r = await client.post(
        f"/projects/{pid}/functions",
        json={
            "api_name": "js1", "display_name": "JS1", "language": "javascript",
            "source_code": "result = 1",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    r = await client.get(
        f"/projects/{pid}/functions?language=javascript",
        headers={"X-User-Email": ALICE},
    )
    items = r.json()
    assert len(items) == 1
    assert items[0]["api_name"] == "js1"


@pytest.mark.asyncio
async def test_get_function_404(client: AsyncClient):
    """不存在的 Function → 404。"""
    pid = await _project(client)
    r = await client.get(
        f"/projects/{pid}/functions/{uuid.uuid4()}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_function_bumps_version_on_code_change(client: AsyncClient):
    """修改 source_code → version +1。"""
    pid = await _project(client)
    fn = await _make_function(client, pid, code="result = 1")
    assert fn["version"] == 1

    r = await client.patch(
        f"/projects/{pid}/functions/{fn['id']}",
        json={"source_code": "result = 2"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["source_code"] == "result = 2"


@pytest.mark.asyncio
async def test_update_function_keeps_version_on_description_only(client: AsyncClient):
    """仅修改 description → version 不变。"""
    pid = await _project(client)
    fn = await _make_function(client, pid)
    r = await client.patch(
        f"/projects/{pid}/functions/{fn['id']}",
        json={"description": "new desc"},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1


@pytest.mark.asyncio
async def test_delete_function(client: AsyncClient):
    """删除 Function → 204 → 再 GET 404。"""
    pid = await _project(client)
    fn = await _make_function(client, pid)
    r = await client.delete(
        f"/projects/{pid}/functions/{fn['id']}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 204
    r = await client.get(
        f"/projects/{pid}/functions/{fn['id']}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


# ===========================================================================
# Test endpoint
# ===========================================================================


@pytest.mark.asyncio
async def test_function_test_python_basic(client: AsyncClient):
    """Python 同步执行：返回 result。"""
    pid = await _project(client)
    fn = await _make_function(
        client, pid, api_name="dbl",
        code="result = input_data.get('x', 0) * 2",
    )
    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {"x": 21}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert body["timed_out"] is False
    assert body["output_data"] == 42
    assert body["function_id"] == fn["id"]
    assert body["version"] == 1
    assert body["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_function_test_python_complex_calc(client: AsyncClient):
    """复杂计算：累计 / 聚合。"""
    pid = await _project(client)
    fn = await _make_function(
        client, pid, api_name="total",
        code="""
items = input_data.get('items', [])
total = sum(i['price'] * i['qty'] for i in items)
result = {'total': total, 'count': len(items)}
""",
    )
    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {
            "items": [{"price": 10, "qty": 3}, {"price": 5, "qty": 7}],
        }},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["output_data"] == {"total": 65, "count": 2}
    assert body["error"] is None


@pytest.mark.asyncio
async def test_function_test_python_runtime_error(client: AsyncClient):
    """运行时错误：错误透传到 error 字段。"""
    pid = await _project(client)
    fn = await _make_function(
        client, pid, api_name="boom",
        code="raise ValueError('intentional')",
    )
    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["output_data"] is None
    assert body["error"] is not None
    assert "ValueError" in body["error"] or "intentional" in body["error"]


@pytest.mark.asyncio
async def test_function_test_timeout(client: AsyncClient):
    """timeout_s 控制 wall-clock 超时。"""
    pid = await _project(client)
    fn = await _make_function(
        client, pid, api_name="slow",
        code="""
import time
time.sleep(10)
result = 'unreachable'
""",
    )
    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {}, "timeout_s": 2},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    assert body["error"] is not None
    # SandboxError 消息包含 "timeout" 字样
    assert "timeout" in body["error"].lower()


@pytest.mark.asyncio
async def test_function_test_writes_function_run(client: AsyncClient):
    """每次 /test 都创建 FunctionRun 审计记录。"""
    pid = await _project(client)
    fn = await _make_function(client, pid)
    for i in range(3):
        r = await client.post(
            f"/projects/{pid}/functions/{fn['id']}/test",
            json={"input_data": {"i": i}},
            headers={"X-User-Email": ALICE},
        )
        assert r.status_code == 200

    # 列出 runs
    r = await client.get(
        f"/projects/{pid}/functions/{fn['id']}/runs",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 3
    # SQLite CURRENT_TIMESTAMP 是秒级精度；同秒创建可能并列，因此按 input_data.i 兜底校验
    inputs = sorted(r["input_data"]["i"] for r in runs)
    assert inputs == [0, 1, 2]


@pytest.mark.asyncio
async def test_function_test_javascript_basic(client: AsyncClient):
    """JavaScript 同步执行。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/functions",
        json={
            "api_name": "jsfn",
            "display_name": "JSFN",
            "language": "javascript",
            "source_code": "const x = (input_data.a || 0) + (input_data.b || 0); result = { sum: x };",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201, r.text
    fn = r.json()

    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {"a": 10, "b": 20}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert body["output_data"] == {"sum": 30}


@pytest.mark.asyncio
async def test_function_test_typescript_not_implemented(client: AsyncClient):
    """TypeScript runtime 未实现 → 错误信息提示。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/functions",
        json={
            "api_name": "tsfn", "display_name": "TSFN", "language": "typescript",
            "source_code": "result = 1",
        },
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 201
    fn = r.json()

    r = await client.post(
        f"/projects/{pid}/functions/{fn['id']}/test",
        json={"input_data": {}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 200
    body = r.json()
    assert "TypeScript" in (body["error"] or "")


@pytest.mark.asyncio
async def test_function_test_404_for_missing(client: AsyncClient):
    """不存在的 Function id → 404。"""
    pid = await _project(client)
    r = await client.post(
        f"/projects/{pid}/functions/{uuid.uuid4()}/test",
        json={"input_data": {}},
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_function_cross_project_isolation(client: AsyncClient):
    """Function 不允许跨项目访问：get 跨 project_id → 404。"""
    pid1 = await _project(client)
    pid2 = await _project(client)
    fn = await _make_function(client, pid1)

    # 用 pid2 路径访问 pid1 的 fn → 404
    r = await client.get(
        f"/projects/{pid2}/functions/{fn['id']}",
        headers={"X-User-Email": ALICE},
    )
    assert r.status_code == 404

"""HIA-49 / HIA-55 / M1-02 集成测试 — 证据收件箱 / 来源定位 / 访问范围。

每个测试用独立的 SQLite 文件 + alembic up head；走 ASGI，不开 socket。
"""
from __future__ import annotations

import io
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


def _csv_payload(rows: list[list[str]]) -> bytes:
    """生成 CSV bytes（不带 BOM）。"""
    buf = io.StringIO()
    for row in rows:
        buf.write(",".join(row) + "\n")
    return buf.getvalue().encode("utf-8")


# ============================================================
# Tests
# ============================================================

@pytest.mark.asyncio
async def test_csv_upload_creates_source_snapshot_and_auto_evidence(client: AsyncClient):
    """HIA-49: CSV 上传 → Source + SourceSnapshot + 每列一条 SOURCE_FIELD 证据。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    csv_bytes = _csv_payload(
        [
            ["batch_id", "product", "qty", "produced_at"],
            ["B001", "Widget", "100", "2026-09-01"],
            ["B002", "Gadget", "50", "2026-09-02"],
            ["B003", "Sprocket", "75", "2026-09-03"],
        ]
    )

    r = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=alice_h,
        files={"file": ("batches.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["row_count"] == 3
    assert body["column_count"] == 4
    assert body["auto_evidence_count"] == 4
    assert len(body["auto_evidence_ids"]) == 4

    source_id = body["source"]["id"]
    assert body["source"]["source_type"] == "csv"
    assert body["source"]["row_count"] == 3
    assert body["source"]["column_count"] == 4

    # 收件箱里能查到 4 条自动证据
    inbox = (
        await client.get(f"/evidences/project/{proj}", headers=alice_h)
    ).json()
    assert len(inbox) == 4
    field_names = {e["field_name"] for e in inbox}
    assert field_names == {"batch_id", "product", "qty", "produced_at"}
    # 字段级 location
    assert all(e["location"].startswith("column:") for e in inbox)

    # 审计里至少有 1 条 source CREATE
    audit = (await client.get(f"/projects/{proj}/audit", headers=alice_h)).json()
    types = [e["event_type"] for e in audit]
    assert "create" in types
    upload_audits = [
        e for e in audit if e["target_type"] == "source"
    ]
    assert upload_audits
    after = upload_audits[0]["after"] or {}
    assert after.get("row_count") == 3
    assert after.get("column_count") == 4
    assert after.get("auto_evidence_count") == 4


@pytest.mark.asyncio
async def test_bob_cannot_read_alice_source_or_evidence(client: AsyncClient):
    """M1-02 访问范围：Bob 跨项目访问 Source / Evidence 必须 404。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    bob_h = {"X-User-Email": EMAIL_BOB}

    alice_proj = await _alice_project(client)
    await _bob_project(client)

    # Alice 上传一个 CSV
    csv_bytes = _csv_payload([["k", "v"], ["1", "a"]])
    upload = await client.post(
        f"/sources/upload?project_id={alice_proj}",
        headers=alice_h,
        files={"file": ("a.csv", csv_bytes, "text/csv")},
    )
    assert upload.status_code == 201
    sid = upload.json()["source"]["id"]
    eid = upload.json()["auto_evidence_ids"][0]

    # Bob 试图列 Alice 的 source
    r = await client.get(
        f"/sources", params={"project_id": alice_proj}, headers=bob_h
    )
    assert r.status_code == 404

    # Bob 试图直接拿 source
    r = await client.get(
        f"/sources/{sid}", params={"project_id": alice_proj}, headers=bob_h
    )
    assert r.status_code == 404

    # Bob 试图拿 evidence 收件箱
    r = await client.get(
        f"/evidences/project/{alice_proj}", headers=bob_h
    )
    assert r.status_code == 404

    # Bob 试图 align 一条 evidence
    r = await client.patch(
        f"/evidences/{eid}/align",
        params={"project_id": alice_proj},
        json={"ontology_class_iri": "ex:Widget"},
        headers=bob_h,
    )
    assert r.status_code == 404

    # Bob 试图 bulk-confirm
    r = await client.post(
        f"/evidences/bulk-confirm",
        params={"project_id": alice_proj},
        json={
            "evidence_ids": [eid],
            "ontology_class_iri": "ex:Widget",
        },
        headers=bob_h,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_evidence_align_writes_audit_with_hash_chain(client: AsyncClient):
    """HIA-55 决策流：align 写入审计；哈希链延伸到 evidence 事件。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    csv_bytes = _csv_payload([["batch_id", "qty"], ["B1", "10"]])
    upload = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=alice_h,
        files={"file": ("b.csv", csv_bytes, "text/csv")},
    )
    eid = upload.json()["auto_evidence_ids"][0]

    r = await client.patch(
        f"/evidences/{eid}/align",
        params={"project_id": proj},
        json={
            "ontology_class_iri": "ex:Batch",
            "property_iri": "ex:batchId",
            "confidence": 0.9,
            "notes": "fits the schema",
        },
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["alignment_confirmed"] is True

    # 审计链：至少有 source/evidence 的 create + align 的 update
    audit = (await client.get(f"/projects/{proj}/audit", headers=alice_h)).json()
    types = [e["event_type"] for e in audit]
    assert "create" in types
    assert "update" in types
    align_events = [
        e for e in audit
        if e["target_type"] == "evidence" and e["event_type"] == "update"
    ]
    assert align_events
    after = align_events[0]["after"]
    assert after["ontology_class_iri"] == "ex:Batch"
    assert after["property_iri"] == "ex:batchId"
    assert after["is_confirmed"] is True

    # hash chain 验证
    verify = (
        await client.get("/audit/verify", params={"project_id": proj})
    ).json()
    assert verify["ok"] is True
    assert verify["checked"] >= 2


@pytest.mark.asyncio
async def test_bulk_confirm_only_touches_target_project(client: AsyncClient):
    """HIA-55 bulk 决策：只更新本项目 evidence，跨项目 ID 视为未匹配。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj_a = await _alice_project(client)
    proj_b = await _alice_project(client)

    up_a = await client.post(
        f"/sources/upload?project_id={proj_a}",
        headers=alice_h,
        files={"file": ("a.csv", _csv_payload([["x"], ["1"]]), "text/csv")},
    )
    up_b = await client.post(
        f"/sources/upload?project_id={proj_b}",
        headers=alice_h,
        files={"file": ("b.csv", _csv_payload([["y"], ["2"]]), "text/csv")},
    )
    eids_a = up_a.json()["auto_evidence_ids"]
    eids_b = up_b.json()["auto_evidence_ids"]

    # 用 project_a 的 scope + 混入 project_b 的 evidence_id，应只更新 project_a
    r = await client.post(
        f"/evidences/bulk-confirm",
        params={"project_id": proj_a},
        json={"evidence_ids": eids_a + eids_b, "ontology_class_iri": "ex:X"},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == len(eids_a)  # 只匹配 project_a 的
    assert body["total"] == len(eids_a) + len(eids_b)


@pytest.mark.asyncio
async def test_viewer_cannot_upload_or_decide(client: AsyncClient):
    """M1-02 访问范围：VIEWER 不能写 Source/Evidence，只能读。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    bob_h = {"X-User-Email": EMAIL_BOB}
    proj = await _alice_project(client)

    r = await client.post(
        f"/projects/{proj}/members",
        json={"email": EMAIL_BOB, "role": "viewer"},
        headers=alice_h,
    )
    assert r.status_code == 201

    # viewer 不能上传
    r = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=bob_h,
        files={"file": ("x.csv", _csv_payload([["a"], ["1"]]), "text/csv")},
    )
    assert r.status_code == 404  # OWNER/EDITOR 才允许；viewer 被 require_role 拦下

    # viewer 不能 align
    # 先让 alice 上传一条 evidence
    up = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=alice_h,
        files={"file": ("y.csv", _csv_payload([["a"], ["1"]]), "text/csv")},
    )
    eid = up.json()["auto_evidence_ids"][0]
    r = await client.patch(
        f"/evidences/{eid}/align",
        params={"project_id": proj},
        json={"ontology_class_iri": "ex:A"},
        headers=bob_h,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_xlsx_upload_requires_openpyxl_or_returns_400(client: AsyncClient):
    """HIA-49 XLSX 路径：缺 openpyxl 时清晰报错（不静默降级）。"""
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)

    # 伪造 4 个字节 zip header 让 mimetype 检查走 XLSX 路径
    r = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=alice_h,
        files={"file": ("data.xlsx", b"PK\x03\x04not-a-real-zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    # 缺少 openpyxl → 400；或 openpyxl 在但 zip 损坏 → 400
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_reject_evidence_writes_audit_and_clears_alignment(client: AsyncClient):
    alice_h = {"X-User-Email": EMAIL_ALICE}
    proj = await _alice_project(client)
    up = await client.post(
        f"/sources/upload?project_id={proj}",
        headers=alice_h,
        files={"file": ("a.csv", _csv_payload([["a"], ["1"]]), "text/csv")},
    )
    eid = up.json()["auto_evidence_ids"][0]

    # 先 align，再 reject
    await client.patch(
        f"/evidences/{eid}/align",
        params={"project_id": proj},
        json={"ontology_class_iri": "ex:A"},
        headers=alice_h,
    )
    r = await client.patch(
        f"/evidences/{eid}/reject",
        params={"project_id": proj},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_confirmed"] is False
    assert body["ontology_class_iri"] is None

    audit = (await client.get(f"/projects/{proj}/audit", headers=alice_h)).json()
    update_events = [
        e for e in audit
        if e["target_type"] == "evidence" and e["event_type"] == "update"
    ]
    assert len(update_events) >= 2  # align + reject
    # 最后一条应该是 reject 的结果（is_confirmed=False）
    last_update = update_events[0]
    assert last_update["after"]["is_confirmed"] is False

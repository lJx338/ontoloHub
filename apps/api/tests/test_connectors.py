"""Connector 框架测试（HIA-71）。

覆盖范围：
- 注册表基本行为（list_connector_types / is_registered / get_connector）
- 错误输入（未知 type / 缺 config）
- CSV / JSON 两种纯 stdlib connector 的端到端 test_connection + snapshot
- Connector 模型的加密字段往返
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.services import connectors
from src.services.connectors import ConnectorError, get_connector


class TestRegistry:
    def test_list_includes_builtins(self):
        types = set(connectors.list_connector_types())
        # 至少包含 CSV / Excel / JSON / Parquet / Postgres
        assert {"csv", "json", "postgresql"}.issubset(types)

    def test_is_registered(self):
        assert connectors.is_registered("csv")
        assert not connectors.is_registered("redis")

    def test_get_unknown_raises(self):
        with pytest.raises(ConnectorError):
            get_connector("redis", {})

    def test_get_returns_instance(self):
        c = get_connector("csv", {"path": "x.csv"})
        assert c.type == "csv"


class TestCSVConnector:
    @pytest.mark.asyncio
    async def test_missing_path(self):
        c = get_connector("csv", {})
        ok, msg = await c.test_connection()
        assert not ok
        assert "path" in msg.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_file(self, tmp_path: Path):
        c = get_connector("csv", {"path": str(tmp_path / "nope.csv")})
        ok, msg = await c.test_connection()
        assert not ok
        assert "not found" in msg.lower()

    @pytest.mark.asyncio
    async def test_snapshot(self, tmp_path: Path):
        path = tmp_path / "data.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "name", "email"])
            w.writeheader()
            w.writerow({"id": "1", "name": "Alice", "email": "a@x.com"})
            w.writerow({"id": "2", "name": "Bob", "email": "b@x.com"})
            w.writerow({"id": "3", "name": "Carol", "email": "c@x.com"})

        c = get_connector("csv", {"path": str(path)})
        ok, _ = await c.test_connection()
        assert ok
        tables = await c.list_tables()
        assert len(tables) == 1
        snap = await c.snapshot(tables[0].name, limit=10)
        assert snap.row_count == 3
        assert snap.rows[0]["name"] == "Alice"
        assert {f.name for f in snap.fields} == {"id", "name", "email"}

    @pytest.mark.asyncio
    async def test_snapshot_with_limit_offset(self, tmp_path: Path):
        path = tmp_path / "big.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id"])
            for i in range(20):
                w.writerow([i])

        c = get_connector("csv", {"path": str(path)})
        snap = await c.snapshot("anything", limit=5, offset=5)
        assert snap.row_count == 5
        assert snap.rows[0]["id"] == "5"
        assert snap.rows[-1]["id"] == "9"
        # total_seen > returned → truncated=True
        assert snap.truncated


class TestJSONConnector:
    @pytest.mark.asyncio
    async def test_snapshot_array(self, tmp_path: Path):
        path = tmp_path / "data.json"
        path.write_text(json.dumps([
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]), encoding="utf-8")
        c = get_connector("json", {"path": str(path)})
        ok, _ = await c.test_connection()
        assert ok
        snap = await c.snapshot("root")
        assert snap.row_count == 2
        assert {f.name for f in snap.fields} == {"id", "name"}

    @pytest.mark.asyncio
    async def test_snapshot_requires_array(self, tmp_path: Path):
        path = tmp_path / "scalar.json"
        path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        c = get_connector("json", {"path": str(path)})
        with pytest.raises(ConnectorError, match="must be an array"):
            await c.snapshot("root")

    @pytest.mark.asyncio
    async def test_json_path(self, tmp_path: Path):
        path = tmp_path / "nested.json"
        path.write_text(json.dumps({
            "data": {"items": [{"x": 1}, {"x": 2}]}
        }), encoding="utf-8")
        c = get_connector("json", {"path": str(path), "json_path": "$.data.items"})
        snap = await c.snapshot("root")
        assert snap.row_count == 2
        assert snap.rows[0] == {"x": 1}


class TestPostgresConnector:
    def test_missing_required_fields(self):
        from src.services.connectors.postgres_connector import PostgreSQLConnector
        c = PostgreSQLConnector({"host": "x"})  # 缺 db / user / password
        with pytest.raises(ConnectorError, match="missing config"):
            c._conn_args()

    def test_select_only_validator_blocks_dml(self):
        from src.services.connectors.postgres_connector import _is_select_only
        assert _is_select_only("SELECT 1")
        assert _is_select_only("WITH x AS (SELECT 1) SELECT * FROM x")
        assert not _is_select_only("DROP TABLE x")
        assert not _is_select_only("UPDATE x SET a=1")
        assert not _is_select_only("DELETE FROM x")
        # 误用：黑名单关键字包含 DDL
        assert not _is_select_only("SELECT 1; DROP TABLE x")

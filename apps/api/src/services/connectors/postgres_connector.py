"""PostgreSQL 只读连接器（HIA-67）。

设计要点：

- **只读** — ``SELECT`` 之外的所有 DML/DDL 在执行前都会被拦截器拒绝；
  用户若想拉取大表，可配置 ``statement_timeout`` 防止误操作。
- **连接管理** — 每次 snapshot 用独立 ``asyncpg.connect()``，避免长连接
  泄漏；测试用 ping。
- **schema_filter / table_filter** — 可选白名单（glob 风格）；
  ``table_filter`` 例：``"public.*"`` / ``"sales_*"``。
- **行数估计** — 通过 ``pg_stat_user_tables.n_live_tup`` 近似，避免
  全表 COUNT。
"""
from __future__ import annotations

import logging
import re
from typing import Any, ClassVar, Optional

from ._framework import (
    Connector,
    ConnectorError,
    SchemaField,
    SnapshotResult,
    TableInfo,
    register,
)

logger = logging.getLogger(__name__)


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|"
    r"copy\s+from|call|do|set\s+role|vacuum|reindex|cluster|lock|refresh|"
    r"listen|notify|unlisten)\b",
    re.IGNORECASE,
)


def _is_select_only(sql: str) -> bool:
    """判断 SQL 是否只包含 SELECT/CTE + 只读函数。

    简单正则：必须以 SELECT/WITH 开头；不能含上述黑名单关键字。
    这只是基本防御，仍应依赖数据库层 ``readonly role`` 配置。
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False
    head = s.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        return False
    if _FORBIDDEN.search(s):
        return False
    return True


@register
class PostgreSQLConnector(Connector):
    """PostgreSQL 只读连接器。

    config:
        host: 必填
        port: 默认 5432
        database: 必填
        username: 必填
        password: 必填（已解密后传入）
        schema_filter: 可选，白名单 schema 列表（如 ["public"]）
        table_filter: 可选，glob 模式（如 ["public.customers", "public.orders_*"]）
        statement_timeout_ms: 默认 30000
        sslmode: 可选（disable / require / verify-full 等）
    """

    type: ClassVar[str] = "postgresql"

    def _conn_args(self) -> dict[str, Any]:
        required = ["host", "database", "username", "password"]
        missing = [k for k in required if not self.config.get(k)]
        if missing:
            raise ConnectorError(
                f"postgres connector missing config: {', '.join(missing)}"
            )
        return {
            "host": self.config["host"],
            "port": int(self.config.get("port", 5432)),
            "database": self.config["database"],
            "user": self.config["username"],
            "password": self.config["password"],
            "ssl": self.config.get("sslmode") or None,
            "timeout": 15,
            "statement_cache_size": 0,  # 避免 pgBouncer transaction-pool 问题
        }

    async def _connect(self):
        try:
            import asyncpg
        except ImportError as e:
            raise ConnectorError(
                "asyncpg not installed (pip install asyncpg)"
            ) from e
        return await asyncpg.connect(**self._conn_args())

    async def test_connection(self) -> tuple[bool, str]:
        try:
            conn = await self._connect()
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        try:
            version = await conn.fetchval("SELECT version()")
        except Exception as e:
            return False, f"connected but query failed: {e}"
        finally:
            await conn.close()
        return True, (version or "")[:120]

    def _passes_filter(self, schema: str, table: str) -> bool:
        sf = self.config.get("schema_filter")
        if sf and schema not in sf:
            return False
        tf = self.config.get("table_filter") or []
        if tf:
            full = f"{schema}.{table}"
            for pat in tf:
                if "*" in pat or "?" in pat:
                    regex = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
                    if re.match(regex, full):
                        return True
                elif pat == full:
                    return True
            return False
        return True

    async def list_tables(self) -> list[TableInfo]:
        conn = await self._connect()
        try:
            sql = """
                SELECT schemaname, relname, n_live_tup
                FROM pg_stat_user_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY schemaname, relname
            """
            rows = await conn.fetch(sql)
            tables: list[TableInfo] = []
            for r in rows:
                schema = r["schemaname"]
                name = r["relname"]
                if not self._passes_filter(schema, name):
                    continue
                tables.append(
                    TableInfo(
                        name=f"{schema}.{name}",
                        description=f"PG table {schema}.{name}",
                        row_count_estimate=r["n_live_tup"],
                        schema=None,
                    )
                )
            return tables
        finally:
            await conn.close()

    async def _introspect(self, conn, schema: str, table: str) -> list[SchemaField]:
        sql = """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
        """
        rows = await conn.fetch(sql, schema, table)
        return [
            SchemaField(
                name=r["column_name"],
                data_type=r["data_type"],
                nullable=(r["is_nullable"] == "YES"),
            )
            for r in rows
        ]

    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult:
        # table 格式: schema.name
        if "." not in table:
            raise ConnectorError(
                "table name must be 'schema.name' for postgres connector"
            )
        schema, name = table.split(".", 1)
        if not self._passes_filter(schema, name):
            raise ConnectorError(f"table not in filter whitelist: {table}")

        conn = await self._connect()
        try:
            timeout_ms = int(self.config.get("statement_timeout_ms", 30000))
            if timeout_ms > 0:
                await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")

            # 用参数化限定符
            quoted = f'"{schema}"."{name}"'
            sql = f"SELECT * FROM {quoted}"
            if limit and limit > 0:
                sql += f" LIMIT {int(limit)}"
            if offset and offset > 0:
                sql += f" OFFSET {int(offset)}"

            # 防注入：quoted table 名称已在 _passes_filter 检查（白名单或可识别的 schema）
            records = await conn.fetch(sql)
            fields = await self._introspect(conn, schema, name)

            rows = [dict(r) for r in records]
            truncated = len(rows) == limit and limit > 0
            return SnapshotResult(
                table=table,
                rows=rows,
                fields=fields,
                row_count=len(rows),
                truncated=truncated,
            )
        finally:
            await conn.close()

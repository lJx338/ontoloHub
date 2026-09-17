"""CSV / Excel / JSON / Parquet 文件型连接器（HIA-71）。"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any, ClassVar, Optional

from ._framework import (
    Connector,
    ConnectorError,
    SchemaField,
    SnapshotResult,
    TableInfo,
    _resolve_path,
    _truncate_rows,
    register,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


@register
class CSVConnector(Connector):
    """本地 CSV 文件连接器。

    config:
        path: 相对 ``~/.ontolohub/data`` 或绝对路径
        encoding: 默认 utf-8
        delimiter: 默认 ','
    """

    type: ClassVar[str] = "csv"

    def _path(self) -> Path:
        p = self.config.get("path")
        if not p:
            raise ConnectorError("csv connector requires config.path")
        return _resolve_path(p)

    async def test_connection(self) -> tuple[bool, str]:
        try:
            path = self._path()
        except ConnectorError as e:
            return False, str(e)
        if not path.exists():
            return False, f"file not found: {path}"
        if not path.is_file():
            return False, f"not a regular file: {path}"
        try:
            with path.open("r", encoding=self.config.get("encoding", "utf-8")) as f:
                reader = csv.reader(f, delimiter=self.config.get("delimiter", ","))
                next(reader, None)
        except Exception as e:
            return False, f"failed to read: {e}"
        return True, ""

    async def list_tables(self) -> list[TableInfo]:
        path = self._path()
        return [
            TableInfo(
                name=path.stem,
                description=f"CSV file at {path}",
                row_count_estimate=None,
                schema=None,
            )
        ]

    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult:
        path = self._path()
        rows: list[dict[str, Any]] = []
        headers: list[str] = []
        total_seen = 0
        try:
            with path.open(
                "r",
                encoding=self.config.get("encoding", "utf-8"),
                newline="",
            ) as f:
                reader = csv.DictReader(
                    f, delimiter=self.config.get("delimiter", ",")
                )
                headers = list(reader.fieldnames or [])
                skipped = 0
                for raw in reader:
                    if offset and skipped < offset:
                        skipped += 1
                        continue
                    total_seen += 1
                    rows.append(dict(raw))
                    if limit and limit > 0 and len(rows) >= limit + offset:
                        break
        except FileNotFoundError as e:
            raise ConnectorError(f"csv file not found: {path}") from e
        truncated = total_seen > len(rows) + offset
        fields = [
            SchemaField(name=h, data_type="string", nullable=True)
            for h in headers
        ]
        return SnapshotResult(
            table=path.stem,
            rows=rows[:limit] if limit else rows,
            fields=fields,
            row_count=len(rows),
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


@register
class ExcelConnector(Connector):
    """Excel 连接器（依赖 openpyxl）。

    config:
        path: 文件路径
        sheet: sheet 名（默认第一个 sheet）
    """

    type: ClassVar[str] = "excel"

    def _path(self) -> Path:
        p = self.config.get("path")
        if not p:
            raise ConnectorError("excel connector requires config.path")
        return _resolve_path(p)

    async def test_connection(self) -> tuple[bool, str]:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return False, "openpyxl not installed (pip install openpyxl)"
        path = self._path()
        if not path.exists():
            return False, f"file not found: {path}"
        return True, ""

    async def list_tables(self) -> list[TableInfo]:
        try:
            import openpyxl
        except ImportError:
            raise ConnectorError("openpyxl not installed")
        wb = openpyxl.load_workbook(self._path(), read_only=True, data_only=True)
        try:
            return [
                TableInfo(
                    name=name,
                    description=f"Excel sheet {name}",
                    row_count_estimate=None,
                    schema=None,
                )
                for name in wb.sheetnames
            ]
        finally:
            wb.close()

    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult:
        try:
            import openpyxl
        except ImportError:
            raise ConnectorError("openpyxl not installed")
        path = self._path()
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if table not in wb.sheetnames:
                raise ConnectorError(f"sheet not found: {table}")
            ws = wb[table]
            rows_iter = ws.iter_rows(values_only=True)
            try:
                headers = [
                    str(c) if c is not None else f"col_{i}"
                    for i, c in enumerate(next(rows_iter))
                ]
            except StopIteration:
                headers = []
            rows: list[dict[str, Any]] = []
            total_seen = 0
            skipped = 0
            for raw in rows_iter:
                if offset and skipped < offset:
                    skipped += 1
                    continue
                total_seen += 1
                row_dict = {}
                for i, h in enumerate(headers):
                    row_dict[h] = raw[i] if i < len(raw) else None
                rows.append(row_dict)
                if limit and limit > 0 and len(rows) >= limit:
                    break
            truncated = total_seen > len(rows) + offset
            return SnapshotResult(
                table=table,
                rows=rows,
                fields=[
                    SchemaField(name=h, data_type="string", nullable=True)
                    for h in headers
                ],
                row_count=len(rows),
                truncated=truncated,
            )
        finally:
            wb.close()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


@register
class JSONConnector(Connector):
    """JSON 文件 / URL 连接器。

    config:
        path / url: 二选一
        json_path: 指向数组的 JSONPath（默认 "$"，整体必须为数组）
    """

    type: ClassVar[str] = "json"

    async def test_connection(self) -> tuple[bool, str]:
        try:
            await self._load_payload()
        except ConnectorError as e:
            return False, str(e)
        return True, ""

    async def _load_payload(self) -> Any:
        path = self.config.get("path")
        url = self.config.get("url")
        if not path and not url:
            raise ConnectorError("json connector requires config.path or config.url")
        if path:
            p = _resolve_path(path)
            if not p.exists():
                raise ConnectorError(f"file not found: {p}")
            try:
                with p.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as e:
                raise ConnectorError(f"failed to parse JSON: {e}") from e
        else:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    payload = r.json()
            except Exception as e:
                raise ConnectorError(f"failed to fetch JSON: {e}") from e

        # 简单 JSONPath 支持：$.a.b 形式
        jpath = self.config.get("json_path", "$")
        if jpath and jpath != "$":
            cur = payload
            for part in jpath.lstrip("$").lstrip(".").split("."):
                if part == "":
                    continue
                if isinstance(cur, list):
                    try:
                        cur = cur[int(part)]
                    except (ValueError, IndexError):
                        raise ConnectorError(f"invalid json_path index: {part}")
                elif isinstance(cur, dict):
                    if part not in cur:
                        raise ConnectorError(f"json_path key not found: {part}")
                    cur = cur[part]
                else:
                    raise ConnectorError(f"json_path cannot traverse {type(cur)}")
            payload = cur

        if not isinstance(payload, list):
            raise ConnectorError("json payload must be an array (or json_path must point to one)")
        return payload

    async def list_tables(self) -> list[TableInfo]:
        return [TableInfo(name="root", description="JSON array root", schema=None)]

    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult:
        payload = await self._load_payload()
        total = len(payload)
        sliced = payload[offset:] if offset else payload
        truncated = total > len(sliced)
        sliced = sliced[:limit] if limit else sliced
        fields: list[SchemaField] = []
        if sliced:
            keys: set[str] = set()
            for row in sliced:
                if isinstance(row, dict):
                    keys.update(row.keys())
            fields = [
                SchemaField(name=k, data_type="auto", nullable=True)
                for k in sorted(keys)
            ]
        return SnapshotResult(
            table="root",
            rows=sliced,
            fields=fields,
            row_count=len(sliced),
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


@register
class ParquetConnector(Connector):
    """Parquet 文件连接器（依赖 pyarrow）。"""

    type: ClassVar[str] = "parquet"

    def _path(self) -> Path:
        p = self.config.get("path")
        if not p:
            raise ConnectorError("parquet connector requires config.path")
        return _resolve_path(p)

    async def test_connection(self) -> tuple[bool, str]:
        try:
            import pyarrow.parquet  # noqa: F401
        except ImportError:
            return False, "pyarrow not installed (pip install pyarrow)"
        path = self._path()
        if not path.exists():
            return False, f"file not found: {path}"
        return True, ""

    async def list_tables(self) -> list[TableInfo]:
        return [TableInfo(name=self._path().stem, description="Parquet file", schema=None)]

    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ConnectorError("pyarrow not installed")
        path = self._path()
        try:
            table_ = pq.read_table(path)
        except Exception as e:
            raise ConnectorError(f"failed to read parquet: {e}") from e

        if offset:
            table_ = table_.slice(offset)
        total = table_.num_rows
        if limit and table_.num_rows > limit:
            table_ = table_.slice(0, limit)
            truncated = True
        else:
            truncated = False

        rows = table_.to_pylist()
        fields = [
            SchemaField(
                name=f.name,
                data_type=str(f.type),
                nullable=f.nullable,
            )
            for f in table_.schema
        ]
        return SnapshotResult(
            table=path.stem,
            rows=rows,
            fields=fields,
            row_count=len(rows),
            truncated=truncated or total > len(rows) + offset,
        )

"""Connector framework package.

import 此包时所有内置 connector 会被注册（通过 ``@register`` 装饰器）。

使用::

    from src.services import connectors
    c = connectors.get_connector("postgres", config_dict)
    ok, msg = await c.test_connection()
    tables = await c.list_tables()
    snap = await c.snapshot("public.customers", limit=500)
"""
from ._framework import (  # noqa: F401  re-export
    Connector,
    ConnectorError,
    SchemaField,
    SnapshotResult,
    TableInfo,
    _glob_match,
    _resolve_path,
    _truncate_rows,
    get_connector,
    is_registered,
    list_connector_types,
    register,
)

# 触发内置 connector 的 @register 装饰器
from . import (  # noqa: F401, E402
    file_connectors,
    postgres_connector,
)

__all__ = [
    "Connector",
    "ConnectorError",
    "SchemaField",
    "SnapshotResult",
    "TableInfo",
    "get_connector",
    "is_registered",
    "list_connector_types",
    "register",
]

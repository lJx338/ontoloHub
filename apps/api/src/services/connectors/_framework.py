"""Connector 框架 — 基类 / 数据结构 / 注册表（HIA-71 / HIA-67）。

使用::

    from src.services import connectors
    c = connectors.get_connector("postgres", config_dict)
    ok, msg = await c.test_connection()
    tables = await c.list_tables()
    snap = await c.snapshot("public.customers", limit=500)

注意：本模块**只**定义基础类与注册表。具体的 connector 实现放在
``file_connectors`` / ``postgres_connector`` 等子模块，并在 ``__init__.py``
里通过 ``import`` 触发 ``@register``。
"""
from __future__ import annotations

import fnmatch
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConnectorError(RuntimeError):
    """连接器执行期间的所有用户可见错误。"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SchemaField:
    """字段元信息。"""

    name: str
    data_type: str = "string"
    nullable: bool = True


@dataclass
class TableInfo:
    """表/Sheet/数组根 等"逻辑表"的信息。"""

    name: str
    description: Optional[str] = None
    row_count_estimate: Optional[int] = None
    schema: Optional[list[SchemaField]] = None


@dataclass
class SnapshotResult:
    """一次 snapshot 的结果。"""

    table: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    fields: list[SchemaField] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(p: str) -> Path:
    """把 ``p`` 解析为绝对路径：

    - ``~`` / ``~xxx`` → 用户家目录
    - 相对路径 → ``~/.ontolohub/data`` 下
    - 绝对路径 → 直接返回
    """
    if not p:
        raise ConnectorError("path is empty")
    expanded = os.path.expanduser(p)
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.home() / ".ontolohub" / "data" / p
    return path


def _truncate_rows(rows: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = list(rows)
    if limit and limit > 0 and len(rows) > limit:
        return rows[:limit]
    return rows


def _glob_match(pattern: str, value: str) -> bool:
    """glob 风格匹配（兼容 ``*`` / ``?``）。"""
    return fnmatch.fnmatchcase(value, pattern)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, type["Connector"]] = {}


def register(cls: type["Connector"]) -> type["Connector"]:
    """装饰器：把 ``Connector`` 子类登记到全局注册表。

    通过 ``cls.type``（ClassVar）作为注册 key。
    """
    t = getattr(cls, "type", None)
    if not t or not isinstance(t, str):
        raise TypeError(
            f"{cls.__name__} must define class var `type: str` to be registered"
        )
    if t in _REGISTRY and _REGISTRY[t] is not cls:
        logger.warning(
            "connector type %r already registered by %s, overwriting with %s",
            t,
            _REGISTRY[t].__name__,
            cls.__name__,
        )
    _REGISTRY[t] = cls
    return cls


def is_registered(type_: str) -> bool:
    return type_ in _REGISTRY


def list_connector_types() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_connector(type_: str, config: dict[str, Any]) -> "Connector":
    """根据 ``type_`` 取出 connector 实例（不执行 IO，只做构造）。"""
    cls = _REGISTRY.get(type_)
    if cls is None:
        raise ConnectorError(
            f"unknown connector type: {type_!r} "
            f"(available: {list_connector_types()})"
        )
    return cls(config=config or {})


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Connector(ABC):
    """所有 connector 的基类。

    子类约定：

    - 定义 ``type: ClassVar[str]`` 作为注册 key；
    - 实现 ``test_connection`` / ``list_tables`` / ``snapshot`` 三个 async 方法；
    - 通过 ``self.config`` 读取连接参数（dict）。
    """

    type: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any]):
        self.config: dict[str, Any] = dict(config or {})

    # ---- 子类必须实现的接口 ----

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """返回 (ok, message)。message 在 ok=False 时是错误原因；ok=True 时可选版本/状态信息。"""

    @abstractmethod
    async def list_tables(self) -> list[TableInfo]: ...

    @abstractmethod
    async def snapshot(
        self,
        table: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> SnapshotResult: ...


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
    "_resolve_path",
    "_truncate_rows",
    "_glob_match",
]

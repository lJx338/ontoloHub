"""Redis 客户端与简易缓存（HIA-64 B1 — 多用户基础设施）。

设计原则：

- **不可用时不阻断** — Redis 连不上时降级为「无缓存」，调用方拿到
  :class:`CacheUnavailable`，业务继续走。
- **同步 ``redis.asyncio``** — 复用 ``asyncio`` 事件循环；连接池由
  ``redis.asyncio.Redis`` 内部管理。
- **单例 + 健康检查** — :func:`get_cache` 返回全局单例；后台任务每 N 秒
  探一次 ``PING``，失败只记日志、不抛。
- **命名空间 + TTL** — 所有 key 自动拼 ``<prefix>:<namespace>:<key>``；
  写入时默认 TTL（60s）防失控。

使用示例::

    from src.core.cache import get_cache, CacheUnavailable

    cache = get_cache()
    try:
        await cache.set("candidates:proj1:hash", payload, ttl=300)
        cached = await cache.get("candidates:proj1:hash")
    except CacheUnavailable:
        # Redis 挂了 — 业务继续，不影响响应
        ...
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from src.core.config import get_settings

logger = logging.getLogger(__name__)

# 单例
_cache: Optional["Cache"] = None
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class CacheUnavailable(RuntimeError):
    """Redis 不可用（连接 / 认证 / 协议错误）— 调用方应降级处理。"""


# ---------------------------------------------------------------------------
# 配置数据类（避免每次都从 settings 拿）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ResolvedConfig:
    url: str
    enabled: bool
    socket_timeout: float
    connect_timeout: float
    healthcheck_interval_s: int
    key_prefix: str


# ---------------------------------------------------------------------------
# Cache 类
# ---------------------------------------------------------------------------


class Cache:
    """异步 Redis 客户端 + 命名空间包装。

    不做复杂抽象 — :meth:`get` / :meth:`set` / :meth:`delete` / :meth:`ping`
    已经覆盖 M1 阶段 90% 场景。如果以后要分布式锁 / stream / pubsub，
    直接拿 ``self.client`` 用 ``redis.asyncio`` 完整能力。
    """

    def __init__(self, cfg: _ResolvedConfig) -> None:
        self._cfg = cfg
        self._pool: Optional[ConnectionPool] = None
        self._client: Optional[Redis] = None
        self._healthy: bool = False
        self._last_ping_failed_at: Optional[float] = None

    # --------------------------------------------------------------- helpers

    @property
    def client(self) -> Redis:
        """返回原生 ``redis.asyncio.Redis``（懒加载）。"""
        if self._client is None:
            self._build_pool()
        assert self._client is not None
        return self._client

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    def _build_pool(self) -> None:
        self._pool = ConnectionPool.from_url(
            self._cfg.url,
            socket_timeout=self._cfg.socket_timeout,
            socket_connect_timeout=self._cfg.connect_timeout,
            health_check_interval=self._cfg.healthcheck_interval_s,
            decode_responses=True,
        )
        self._client = Redis(connection_pool=self._pool)

    def _key(self, namespace: str, key: str) -> str:
        return f"{self._cfg.key_prefix}:{namespace}:{key}"

    # --------------------------------------------------------------- ops

    async def ping(self) -> bool:
        """发送 ``PING``，更新 ``is_healthy`` 状态。失败不抛。"""
        if not self._cfg.enabled:
            return False
        try:
            pong = await self.client.ping()
            self._healthy = bool(pong)
            self._last_ping_failed_at = None
            return self._healthy
        except RedisError as e:
            if self._last_ping_failed_at is None:
                logger.warning("Redis ping failed: %s", e)
            import time
            self._last_ping_failed_at = time.monotonic()
            self._healthy = False
            return False

    async def get(self, namespace: str, key: str) -> Optional[str]:
        """取值；不可用时抛 :class:`CacheUnavailable`（调用方降级）。"""
        if not self._cfg.enabled:
            raise CacheUnavailable("cache disabled")
        try:
            return await self.client.get(self._key(namespace, key))
        except RedisError as e:
            raise CacheUnavailable(str(e)) from e

    async def set(
        self,
        namespace: str,
        key: str,
        value: str,
        *,
        ttl: int = 60,
    ) -> bool:
        """写入值，默认 TTL 60s；返回是否成功。"""
        if not self._cfg.enabled:
            raise CacheUnavailable("cache disabled")
        try:
            return bool(
                await self.client.set(
                    self._key(namespace, key), value, ex=max(1, ttl)
                )
            )
        except RedisError as e:
            raise CacheUnavailable(str(e)) from e

    async def delete(self, namespace: str, key: str) -> int:
        """删除给定 key，返回删除条数。"""
        if not self._cfg.enabled:
            raise CacheUnavailable("cache disabled")
        try:
            return int(await self.client.delete(self._key(namespace, key)))
        except RedisError as e:
            raise CacheUnavailable(str(e)) from e

    async def get_json(self, namespace: str, key: str) -> Optional[Any]:
        """取 JSON 反序列化。"""
        raw = await self.get(namespace, key)
        if raw is None:
            return None
        import json
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        ttl: int = 60,
    ) -> bool:
        """写 JSON 序列化。"""
        import json
        return await self.set(
            namespace, key, json.dumps(value, default=str), ttl=ttl
        )

    async def incr(self, namespace: str, key: str, *, ttl: int = 60) -> int:
        """自增 +1 并设置 TTL；用于限流。"""
        if not self._cfg.enabled:
            raise CacheUnavailable("cache disabled")
        try:
            pipe = self.client.pipeline()
            pipe.incr(self._key(namespace, key))
            pipe.expire(self._key(namespace, key), max(1, ttl))
            results = await pipe.execute()
            return int(results[0])
        except RedisError as e:
            raise CacheUnavailable(str(e)) from e

    async def aclose(self) -> None:
        """关闭连接池 + 客户端。FastAPI lifespan 关闭时调用。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:  # pragma: no cover
                logger.debug("Redis close error (ignored): %s", e)
        if self._pool is not None:
            try:
                await self._pool.aclose()
            except Exception as e:  # pragma: no cover
                logger.debug("Redis pool close error (ignored): %s", e)
        self._client = None
        self._pool = None


# ---------------------------------------------------------------------------
# 单例 + 健康检查任务
# ---------------------------------------------------------------------------


async def get_cache() -> Cache:
    """获取全局 ``Cache`` 单例（懒初始化）。"""
    global _cache
    async with _lock:
        if _cache is None:
            settings = get_settings().redis
            cfg = _ResolvedConfig(
                url=settings.url,
                enabled=settings.enabled,
                socket_timeout=settings.socket_timeout,
                connect_timeout=settings.connect_timeout,
                healthcheck_interval_s=settings.healthcheck_interval_s,
                key_prefix=settings.key_prefix,
            )
            _cache = Cache(cfg)
            await _cache.ping()
        return _cache


async def reset_cache() -> None:
    """测试 / 重启时重置单例。"""
    global _cache
    async with _lock:
        if _cache is not None:
            await _cache.aclose()
        _cache = None


async def ping_cache_loop(stop_event: asyncio.Event, interval: float = 30.0) -> None:
    """后台循环：每 ``interval`` 秒 ping 一次 Redis。

    只更新健康状态；不抛异常。让 FastAPI lifespan 创建任务、退出时设置
    ``stop_event``。
    """
    cache = await get_cache()
    while not stop_event.is_set():
        try:
            await cache.ping()
        except Exception as e:  # pragma: no cover - 兜底
            logger.debug("cache ping loop error (ignored): %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

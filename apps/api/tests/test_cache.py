"""HIA-64 B1 — Redis 缓存模块测试。

设计：

- 用 ``fakeredis.aioredis`` 模拟 Redis，避免本地跑测试时强制启动真 Redis。
- 验证：单例、命名空间 prefix、TTL、JSON helper、不可用时降级。
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from src.core import cache as cache_mod
from src.core.cache import Cache, CacheUnavailable


# ---------- fixtures ----------


@pytest_asyncio.fixture
async def fake_cache(monkeypatch):
    """注入 fakeredis 替换真 Redis。

    直接 monkeypatch ``Cache._build_pool``：让 ``client`` 属性返回 fake 客户端，
    跳过 ``ConnectionPool.from_url`` 这一步。
    """
    try:
        import fakeredis.aioredis as fakeredis_aioredis
    except ImportError:  # pragma: no cover
        pytest.skip("fakeredis not installed")

    fake_client = fakeredis_aioredis.FakeRedis(decode_responses=True)

    def _fake_build_pool(self) -> None:
        # 把内部 _client 直接挂成 fake，跳过真 Redis 连接
        self._client = fake_client
        self._pool = None

    monkeypatch.setattr(Cache, "_build_pool", _fake_build_pool)

    # 重置单例
    await cache_mod.reset_cache()
    yield fake_client
    await cache_mod.reset_cache()


# ---------- tests ----------


@pytest.mark.asyncio
async def test_get_set_basic(fake_cache):
    cache = await cache_mod.get_cache()
    assert await cache.set("ns", "k1", "v1", ttl=60) is True
    assert await cache.get("ns", "k1") == "v1"


@pytest.mark.asyncio
async def test_get_missing_returns_none(fake_cache):
    cache = await cache_mod.get_cache()
    assert await cache.get("ns", "missing") is None


@pytest.mark.asyncio
async def test_namespace_prefix(fake_cache):
    """同名 key 在不同 namespace 下互不干扰。"""
    cache = await cache_mod.get_cache()
    await cache.set("alpha", "k", "1")
    await cache.set("beta", "k", "2")
    assert await cache.get("alpha", "k") == "1"
    assert await cache.get("beta", "k") == "2"


@pytest.mark.asyncio
async def test_set_json_roundtrip(fake_cache):
    cache = await cache_mod.get_cache()
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    await cache.set_json("ns", "json-key", payload, ttl=60)
    got = await cache.get_json("ns", "json-key")
    assert got == payload


@pytest.mark.asyncio
async def test_get_json_invalid_returns_none(fake_cache):
    cache = await cache_mod.get_cache()
    await cache.set("ns", "bad", "not json{")
    assert await cache.get_json("ns", "bad") is None


@pytest.mark.asyncio
async def test_delete(fake_cache):
    cache = await cache_mod.get_cache()
    await cache.set("ns", "k", "v")
    assert await cache.delete("ns", "k") == 1
    assert await cache.get("ns", "k") is None


@pytest.mark.asyncio
async def test_incr_with_ttl(fake_cache):
    cache = await cache_mod.get_cache()
    assert await cache.incr("rate", "user1", ttl=60) == 1
    assert await cache.incr("rate", "user1", ttl=60) == 2
    assert await cache.incr("rate", "user1", ttl=60) == 3


@pytest.mark.asyncio
async def test_unavailable_raises_when_disabled(monkeypatch):
    """``enabled=False`` 时所有读写都抛 ``CacheUnavailable``。"""
    await cache_mod.reset_cache()

    # 通过直接构造 Cache 测：settings 不重要，传入 disabled 配置
    from src.core.cache import _ResolvedConfig

    cfg = _ResolvedConfig(
        url="redis://nowhere:6379/0",
        enabled=False,
        socket_timeout=1.0,
        connect_timeout=1.0,
        healthcheck_interval_s=30,
        key_prefix="ontolohub",
    )
    c = Cache(cfg)
    with pytest.raises(CacheUnavailable):
        await c.get("ns", "k")
    with pytest.raises(CacheUnavailable):
        await c.set("ns", "k", "v")
    assert await c.ping() is False


@pytest.mark.asyncio
async def test_ping_returns_false_on_error(monkeypatch):
    """Redis 连不上时 ping 不抛，返回 False。"""
    from src.core.cache import _ResolvedConfig

    cfg = _ResolvedConfig(
        url="redis://nowhere.invalid:6379/0",
        enabled=True,
        socket_timeout=0.1,
        connect_timeout=0.1,
        healthcheck_interval_s=30,
        key_prefix="ontolohub",
    )
    c = Cache(cfg)
    assert await c.ping() is False
    assert c.is_healthy is False


@pytest.mark.asyncio
async def test_get_cache_singleton(fake_cache):
    """多次 ``get_cache`` 返回同一个实例。"""
    c1 = await cache_mod.get_cache()
    c2 = await cache_mod.get_cache()
    assert c1 is c2


@pytest.mark.asyncio
async def test_key_format_with_prefix(fake_cache):
    """key 格式：``<prefix>:<namespace>:<key>``。"""
    from src.core.cache import _ResolvedConfig

    await cache_mod.reset_cache()
    # 重新初始化单例并改 prefix — 通过 fake 客户端验证 key 真的拼上了
    # 但因为 _build_pool 已 monkeypatch，重新构造 Cache 即可
    cfg = _ResolvedConfig(
        url="redis://fake:6379/0",
        enabled=True,
        socket_timeout=1.0,
        connect_timeout=1.0,
        healthcheck_interval_s=30,
        key_prefix="myprefix",
    )
    c = Cache(cfg)
    c._build_pool()  # uses monkeypatched version → fake_client
    await c.set("ns", "k", "v")
    assert await fake_cache.get("myprefix:ns:k") == "v"
    assert await c.get("ns", "k") == "v"


@pytest.mark.asyncio
async def test_set_negative_ttl_clamped(fake_cache):
    """TTL<=0 被夹到 1（Redis SET EX 要求 >=1）。"""
    cache = await cache_mod.get_cache()
    await cache.set("ns", "k", "v", ttl=0)
    ttl = await fake_cache.ttl("ontolohub:ns:k")
    assert ttl >= 1

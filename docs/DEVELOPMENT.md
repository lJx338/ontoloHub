# 开发规范与避坑指南

> 给 OntoloHub 工程师的快速参考。新人 15 分钟扫一遍能避开 80% 的常见坑；老同事新接 Linear 卡时也建议先翻这一份。

## 0. 怎么读这份文档

- §1–§5 是「约定」，必须遵守；CI / 评审会卡。
- §6–§9 是「踩过的坑」，了解即可，遇到再回查。
- 凡是文档里没说清楚的新规则，先提 PR 改这份文件，再写代码。

---

## 1. 仓库与提交流程

### 1.1 分支与卡

- 主干分支：`main`。所有 Linear 卡在自己的 feature 分支上做，命名 `<linear-id>-<短描述>`（例：`HIA-49-evidence-upload`）。
- 提交粒度：每个 commit 对应一个语义原子（一次模型改动 / 一个端点 / 一次重构）。禁止 "WIP" / "fix" 单独成 commit。
- Commit 信息：`<scope>: <imperative>`，例 `evidence: add CSV upload + auto-evidence`。
- PR：必须链接到对应 Linear 卡（描述里写 `Closes HIA-XX`），CI 全绿 + 至少 1 名 reviewer 通过才能 merge。

### 1.2 仓库布局

```
apps/
  api/             # FastAPI 后端（核心交付物）
  web/             # React + Vite 前端
  cli/             # ontolohub 命令行（备份 / 诊断 / 种子）
packages/
  core/            # Python 公共库（未来多服务共享，目前少用）
docs/              # 文档（架构 / 数据模型 / 里程碑 / API / 上手）
scripts/           # 工程脚本（dev / clean / init_db / 一次性 debug）
prototype/         # 设计原型，独立维护，不进生产
```

**新增代码落到哪**：

| 改的东西 | 落点 |
|---|---|
| 新 HTTP 端点 | `apps/api/src/api/<domain>.py` |
| 新数据表 / 改字段 | `apps/api/src/db/<domain>.py` + `apps/api/alembic/versions/<日期>_<序号>_<描述>.py` |
| 配置项 | `apps/api/src/core/config.py` + 仓库根 `.env.example` |
| 前端页面 | `apps/web/src/pages/<PageName>.tsx` |
| 跑一次的诊断脚本 | `scripts/_debug_*.py`（一次性，用完即删） |
| 永久命令 | `apps/cli/src/ontolohub_cli/` |

---

## 2. 后端约定（FastAPI + SQLAlchemy 2）

### 2.1 全异步

- 所有 DB 操作 `async def` + `AsyncSession`；没有同步路径。
- 路由 handler 不允许直接 `time.sleep` / `requests.get` 等阻塞调用。
- 不允许 `session.execute(...)` 后忘了 `await`（Pyright 会标红）。

### 2.2 模型与 Mixin

- 所有表继承 `Base, UUIDMixin, TimestampMixin`；需要软删再加 `SoftDeleteMixin`。
- 字段声明统一用 `Mapped[T]` + `mapped_column(...)`，**不要**回到 `Column(...)` 老风格。
- 枚举字段：`Column(SQLEnum(MyEnum), ...)`，DB 里存的是 `.value`（小写字符串），不要写大写。
- 时间字段：`DateTime(timezone=True)`；用 `datetime.now(timezone.utc)`，**不要**用 `datetime.utcnow()`（Python 3.12+ 警告，已计划弃用）。
- 主键：UUID（`UUID(as_uuid=True)`），不要用自增 INT。

### 2.3 认证 / 授权 / 隔离（HIA-51 三件套）

> 三件套：`get_current_user` → `require_role(min)` → `record_audit(...)`。**每次写新端点都要按顺序想一遍**。

- 认证优先级（HIA-64 B1 起）：`Authorization: Bearer <jwt>` → `X-API-Key: ont_xxx` → `X-User-Email` → `X-User-Id` → bootstrap admin。前两个走 JWT/API Key 路径（生产），后三个是 dev header fallback（向后兼容 HIA-51 单机阶段）。详见 §15。
- 授权：每个项目级端点必须挂 `require_role(MIN_ROLE)`；OWNER 才能写成员，EDITOR 才能写业务对象，VIEWER 只能读。
- 隔离：路径中拿到 `project_id` 后，**所有查询必须再 WHERE `project_id == ?`**；helper `_load_xxx_for_project(session, id=..., project_id=...)` 强制这件事，禁止裸用 `select(Foo).where(Foo.id == id)`。
- 失败语义：**不足权限 → 404，不返 403**。这是 M1-10 退出条件，避免 "项目是否存在" 侧信道泄漏。
- 审计：每个**变更**端点（POST / PATCH / DELETE）必须 `await record_audit(...)`，写 before/after + 哈希链；读端点不写。`record_audit` 的 `project_id` 是 keyword-only **可选**（None 表示全局事件，如 login / api_key 撤销）。
- 已知避坑：`require_role` 内部把 `project_id` 当 **Path** 读。如果你的路由用 `project_id` 作为 **Query** 参数，必须用 `require_role_query(MIN_ROLE)`（见 `apps/api/src/api/auth.py`）。

### 2.4 错误处理

- 业务校验失败：抛 `HTTPException(status_code=..., detail="...")`；不要返回 200 + `{ok: false}`。
- 全局兜底 `@app.exception_handler(Exception)` 在 `main.py`，只负责 500 + 不泄漏堆栈。
- 不要捕获 `Exception` 然后 `print(e)` 当作日志；要写 `logging.getLogger(__name__).warning(...)`。

### 2.5 Pydantic 模型

- Request / Response 模型分开定义；不要把 ORM 实例直接返回（会出现 `MissingGreenlet` 或循环引用）。
- DB → Response：手写 `_to_response(obj)` 或 `_xxx_to_response(obj)`，显式字段映射。
- 字段命名：`snake_case`；时间字段序列化为 ISO 字符串（`obj.created_at.isoformat() if obj.created_at else ""`）。

---

## 3. 数据库与 Alembic

### 3.1 迁移规范

- 每次改 schema 必须有新的 revision 文件：**禁止手工 ALTER**。
- 命名：`apps/api/alembic/versions/YYYY_MM_DD_NNNN_<snake_desc>.py`，NNNN 从已有最大值 +1。
- 内容要求：
  - `upgrade()` / `downgrade()` 必须对称（downgrade 即使没人跑也得对）。
  - 列加 nullable 必须显式 `nullable=True`；列改 NOT NULL 必须先 backfill 再 drop default。
  - 字段名变更用 `op.alter_column`；不要 drop+create，丢数据。
- autogenerate 是起点，**不是终稿**：跑出来 diff 后人工审一遍，避免把无关表带上。

### 3.2 SQLite vs PostgreSQL

- 本地默认 SQLite（`apps/api/.dev-data/dev.db` 或测试用 `:memory:`），CI 跑全测试；M1 切 PostgreSQL 后必须两套都过。
- 任何不兼容 SQLite 的特性（`JSONB` 路径、并发索引、`now()` 微秒精度），迁移里要写 `if op.get_bind().dialect.name == "postgresql":`。

---

## 4. 测试约定

### 4.1 后端测试（pytest + httpx）

- 每个测试函数**独立的 SQLite 文件** + `alembic upgrade head`，互不污染。`tmp_path / "test.db"` + `monkeypatch.setenv("DATABASE_URL", ...)`。
- 设置完 env 必须做三件事，缺一个就拿到错的 DB：
  1. `cfg.get_settings.cache_clear()`
  2. `await conn.reinit_engines()`
  3. `async with async_session_factory() as s: await ensure_bootstrap_admin()`
- 客户端用 `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")`，**不要** 用 `TestClient`（它是同步包装，会和 async fixture 抢 loop）。
- 用户身份用 `headers={"X-User-Email": ...}`，不要去硬塞 cookie。

**`isolated_app` fixture 标准模板**（见 §6.31 / §6.32）：

```python
import sys
from pathlib import Path
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

# ⚠️ 切勿在模块级别 import `async_session_factory`：它在 reinit_engines 之前
# 已绑定到默认 DB 引擎。fixture 必须重新拉取（见下 `isolated_app`）。
# 模块级先放一个占位 None；helper 函数都通过这个名引用 — 切勿在这里初始化。
async_session_factory = None  # type: ignore[assignment]

from src.api.<domain> import router  # noqa: E402  路由等无副作用模块可正常导入


@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    global async_session_factory
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from src.core import config as cfg
    cfg.get_settings.cache_clear()                                # 1. 清 settings 缓存

    from src.db import connection as conn
    await conn.reinit_engines()                                    # 2. 重建 engine / factory
    async_session_factory = conn.async_session_factory             # 3. 把全局 rebind 到新 factory

    async with async_session_factory() as s:
        await ensure_bootstrap_admin(s)

    from src.main import app                                       # noqa: E402  app 在 reinit 之后 import
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def auth_headers(isolated_app):
    # 登录拿 token（开发环境用 bootstrap admin / X-User-Email 即可）
    return {"X-User-Email": "alice@example.com"}
```

写入新测试模块时**直接复制这段模板**，再补自己的 router / 业务 fixture；不要
自己重新拼装三步（cache_clear + reinit + rebind）— 见 §6.31。

### 4.2 必须覆盖的负例

新加一个端点时，最少要写：

1. **happy path** — 正确用户、正确角色、正确数据 → 期望 2xx。
2. **跨项目隔离** — 别人项目同 id 的对象 → 404。
3. **角色不足** — VIEWER 试图写 → 404（不是 403）。
4. **审计** — 写操作后在 `/projects/{id}/audit` 能查到；`/audit/verify?project_id=...` 返回 `ok=true`。
5. **入参校验** — 缺字段 / 类型错 → 422 而不是 500。

### 4.3 不做的事

- 不要 `sleep` 等异步事件；用 polling 等待并设置 timeout。
- 不要在测试里直接 `asyncio.run(...)`；用 `@pytest.mark.asyncio`。
- 不要 mock 数据库 — 用真 SQLite + alembic。Mock 会让隔离 bug 漏到生产。

### 4.4 Connector 框架测试

Connector 有两类测试：

1. **单元测试**（`tests/test_connectors.py`）：测试注册表、CSV/JSON/PG connector
   实现，不走 DB、不走 API，直接 `get_connector()` 实例调用 async 方法。

2. **集成测试**（`tests/test_connectors_api.py`）：走 httpx AsyncClient，覆盖 API
   端到端流程。**必须**先用 `isolated_app` fixture 创建独立 SQLite DB，再调用
   `ensure_bootstrap_admin()`，最后注入 `X-User-Id` header。

```python
# connector 单元测试模板
import pytest
from src.services.connectors import get_connector

@pytest.mark.asyncio
async def test_csv_snapshot(tmp_path):
    path = tmp_path / "test.csv"
    path.write_text("id,name\n1,Alice\n2,Bob")
    c = get_connector("csv", {"path": str(path)})
    ok, msg = await c.test_connection()
    assert ok
    snap = await c.snapshot(path.stem, limit=1)
    assert snap.row_count == 1
```

---

## 5. 前端约定（apps/web）

- 路由：每个 page 一个文件，放 `apps/web/src/pages/`，`router.tsx` 注册。
- API 调用：放 `src/api/`（按领域切），用 `fetch` 或 SWR；不要在组件里直接 `fetch('/api/...')`。
- 类型：从 OpenAPI 自动生成 (`apps/api/openapi.json`)；不要手抄。
- 状态：M1 不引 Redux；组件本地 `useState` + SWR 足够。
- Header：调用后端必须带 `X-User-Email`（开发期）；生产由 OIDC 层注入。

---

## 6. 排错常见坑

下面这些坑都在这个仓库里真实发生过。**新人在自己撞上一次之前先扫一遍**。

### 6.1 PowerShell `Set-Content` 把 UTF-8 写成 ANSI

**症状**：写完 Python 文件后，启动报 `SyntaxError: unterminated string literal`，且错误里的中文字符变成 `����`。

**原因**：PowerShell 5.1 默认 `Set-Content` / `Out-File` 用系统 ANSI 编码（CP936 / CP1252），会把 UTF-8 多字节序列截断。

**规避**：

- **永远用编辑器工具（Write / StrReplace）写代码**，不要 `Set-Content`。
- 调试脚本如果非要 Set-Content，加 `-Encoding UTF8`。
- 已经写坏的文件：用 `Read` / `Write` 重写一遍即可修。

### 6.2 FastAPI `Depends(require_role)` 与路由签名不匹配

**症状**：调用端点拿到 `422 {"detail":[{"type":"missing","loc":["path","project_id"]...}]}`。

**原因**：`require_role(Role.X)` 内部把 `project_id` 当 **Path 参数** 读（`Path(..., description="project scope")`）。如果你的路由声明的是 `project_id = Query(...)`，FastAPI 会拒绝。

**规避**：

- 路由用 **Path** 风格（推荐，挂在 `/projects/{project_id}/...` prefix 下），用 `require_role(MIN_ROLE)`。
- 路由用 **Query** 风格（端点不在项目路径下），用 `require_role_query(MIN_ROLE)`（已加在 `apps/api/src/api/auth.py`）。
- 写新路由时如果搞混，TypeError 不会立刻抛，是运行时报 422；写完跑一次集成测试就能发现。

### 6.3 httpx multipart 上传手动塞 `Content-Type` 把 boundary 弄丢

**症状**：`files={...}` 上传返回 `400 {"detail":"Missing boundary in multipart."}`。

**原因**：`Content-Type: multipart/form-data` 必须带 `boundary=...`，这个 boundary 由 httpx 自动生成。手写 header 把自动生成的覆盖了。

**规避**：

```python
# 错
headers = {**alice_h, "Content-Type": "multipart/form-data"}
await client.post(url, headers=headers, files={"file": ...})

# 对
await client.post(url, headers=alice_h, files={"file": ...})
```

### 6.4 SQLAlchemy `flush()` 后访问属性触发 MissingGreenlet / expired

**症状**：`coerce_diff(obj)` 在 `await session.flush()` 之后抛 `MissingGreenlet: ... lazy load ...` 或读到旧值。

**原因**：`flush()` 之后 SQLAlchemy 默认 expire 所有属性，下次访问时会发新的 SELECT。在 async session 里这次隐式 SELECT 会触发 `MissingGreenlet`。

**规避**：写审计 / diff 之前先 `await session.refresh(obj)` 一次。已经在 `apps/api/src/api/members.py` 里这么做了，**新加的写端点必须照抄**。

### 6.5 SQLite `func.now()` 只有秒级精度，破坏审计哈希链

**症状**：同一秒内写两条审计，`verify_chain` 把后一条判定为断链（prev_hash 期望的前一条 entry_hash 还没落库）。

**原因**：`func.now()` 是 server_default；SQLite 上是 `CURRENT_TIMESTAMP` 秒级精度。

**规避**：审计时间在 Python 侧生成并写到对象：

```python
entry.created_at = _now_utc()        # datetime.now(timezone.utc) 微秒精度
await session.flush()
```

`_now_utc()` 已封装在 `apps/api/src/api/auth.py`。**所有 `AuditEvent` 都这么写**。

### 6.6 异步 session 里 `record_audit` 之后忘了 `flush` / `commit`

**症状**：端点返 200，但 `GET /projects/{id}/audit` 查不到这条事件。

**原因**：`record_audit` 内部 `session.flush()` 但 **不 commit**；事务边界由 handler 的依赖 `get_session` 控制。如果端点路径上再包了一层 `with begin():` 但没把 session 传进去，或者 handler 提前 return，事件会跟着 transaction rollback。

**规避**：

- 写端点用 `session: AsyncSession = Depends(get_session)`，**不要自己开 transaction**。
- 改完查 `/audit/verify?project_id=...` 必须 `ok=true` 才能合并。

### 6.7 Alembic revision 文件名格式不对

**症状**：`alembic upgrade head` 报 `Can't locate revision identified by '...'`，或根本没看到这个文件。

**原因**：文件名必须是 `YYYY_MM_DD_NNNN_<desc>.py`，且 NNNN 严格按时间排序。`autogenerate` 默认按 UTC 时间给前缀，时区不对会让它排到未来去。

**规避**：

- 手写 revision 时 NNNN 取当前 repo 内最大 +1。
- 跑 `alembic history` 看是不是被识别了；没识别就是文件名错。

### 6.8 测试里 `monkeypatch.setenv("DATABASE_URL", ...)` 没生效

**症状**：测试拿到的还是 .env 里那个 PG URL，跑到一半报连不上。

**原因**：`Settings` 用 `lru_cache`；env 改了它不会重读。`async_engine` 也是模块级单例。

**规避**（按这个顺序，三步缺一不可）：

```python
monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

from src.core import config as cfg
cfg.get_settings.cache_clear()                  # 1. 清除 settings 缓存

from src.db import connection as conn
await conn.reinit_engines()                      # 2. 重建 engine / session factory

from alembic import command
command.upgrade(alembic_cfg, "head")             # 3. 在新 DB 上跑迁移
```

### 6.9 跨项目读对象时漏带 `project_id` 过滤

**症状**：单元测试用 alice 创建对象，bob 用同 id 调接口，**居然拿到 200**。这是隔离 bug，但只在跨项目测试时才暴露。

**原因**：路由签名是 `get_source(source_id)`，内部 `select(Source).where(Source.id == source_id)` — 忘了加 `Source.project_id == project_id`。

**规避**：

- 路由统一用 `_load_xxx_for_project(session, id=..., project_id=...)` helper；helper 内部同时过滤两个字段。
- 测试集里固定包含 `test_<resource>_cross_project_404` 负例；不要省。

### 6.10 改了 ORM 模型但忘了写 Alembic 迁移

**症状**：本地 dev 跑得通，但 `alembic upgrade head` 之后启动报 `OperationalError: no such column`。

**原因**：本地 SQLite 是 dev 跑出来的旧 schema；CI 跑空 DB + upgrade head 才会暴露。

**规避**：

- 改完 ORM 必须 `alembic revision --autogenerate -m "..."` 一次，对比 diff，提交。
- CI 的工作流里至少跑一次 `alembic upgrade head` 后再启动后端。

### 6.11 `_framework.py` 不能从 `__init__.py` 倒导（循环导入）

**症状**：`ImportError: cannot import name 'Connector' from partially initialized module ... circular import`

**原因**：`__init__.py` 里的 `from ._framework import Connector` 触发 `_framework.py` 执行，
而 `_framework.py` 又 `from .__init__ import Connector` — 死循环。

**规避**：`_framework.py` 必须包含**所有**实际定义（`Connector` ABC、`register` 装饰器、
dataclass）。`__init__.py` 只做 re-export + 触发 `@register`。
**永远不要**在 `_framework.py` 里写 `from .__init__ import ...`。

### 6.12 SQLAlchemy mixin 里定义的字段不能和子类显式字段重复

**症状**：`ArgumentError: Column 'description' is already present in this mapping.`

**原因**：`DescriptionMixin.description` 在 mixin 里定义了，`Connector.description` 又在子类
体里定义了一次，SQLAlchemy 试图映射两次。

**规避**：Mixin 只用来提供**多个类共享的字段**。如果某字段只在一个类里用，
直接写到类体里，不走 mixin。Connector 只从 `Base, UUIDMixin, TimestampMixin,
SoftDeleteMixin` 继承，按需显式定义自己的 `description` / `created_by`。

### 6.13 CSV `limit` / `offset` 截断逻辑

**症状**：`snapshot(limit=5, offset=5)` 实际返回 10 行（offset 被吃掉）。

**原因**：错误的 break 条件 `len(rows) >= limit + offset` 导致多读了 offset 那么多行。

**规避**：CSV snapshot 全量读入后用 Python list 切片（`rows[offset:offset+limit]`），
不要在迭代中途做 break 判断。`truncated = total > len(rows)`。

### 6.14 Redis 缓存失败时不应让请求整体失败

**症状**：Redis 重启 / 网络抖动时，本来能 200 的端点开始返 500。

**原因**：缓存层 `RedisError` 直接冒泡到路由层；上游不知道降级。

**规避**：

```python
try:
    cached = await cache.get_json("mapping", key)
except CacheUnavailable:
    cached = None  # 业务继续
```

`src.core.cache.CacheUnavailable` 是统一的「降级信号」。**所有写缓存的代码都
要 try/except**，不要让缓存故障级联到上游。读取也要 catch — 否则瞬时网络
抖动就能让正常请求失败。

### 6.15 测试里直接构造 `Cache` 实例时要重置全局单例

**症状**：上一个测试的单例连接池泄露到下一个测试，连接耗尽。

**原因**：`get_cache()` 用 ``global _cache`` 单例；测试如果直接 ``Cache(cfg)`` 操作
而不 ``reset_cache()``，下次 ``get_cache()`` 仍返回旧实例。

**规避**：fixture 必须成对 — setup 时 ``await cache_mod.reset_cache()``，teardown
再 ``await cache_mod.reset_cache()``。monkeypatch `_build_pool` 时尤其重要，
否则 fake 客户端永远装不到单例上。

### 6.16 把同步函数改成异步后，所有调用方都得改

**症状**：把 `def validate(...)` 改成 `async def validate(...)` 之后，路由
handler / 测试 / 内部调用忘了加 `await`，报 `'coroutine' object has no attribute ...`。

**原因**：Python async 改造不是类型签名变一下就完 — 调用方必须 `await`，包括：
路由 handler（已经是 async，加 `await` 即可），**测试方法**（要改成 `async def` +
`@pytest.mark.asyncio`），**任何同步工具函数**（如果它会调到这个函数，自己也得
变成 async）。

**规避**：

1. 改函数前先 `Grep` 全文 `func_name(` 找出所有调用点。
2. 路由 handler：加 `await`。
3. 测试：`def` → `async def`，加 `@pytest.mark.asyncio`。
4. 第三方库同步 API 包了异步函数时，用 `asyncio.to_thread(...)`。
5. 改完跑完整测试套件，不要只跑你改的那个测试文件 — 调用链上的别处也会炸。

### 6.17 缓存 helper 写进 dict / set 的 key 时要做 hash

**症状**：把整个 `field` dict 直接拼进 cache key（如
`f"profile:{field}"`），结果 key 长到 Redis 报警，或被截断导致不同 field 撞 key。

**原因**：dict 的 `repr()` 包含空格、换行、Unicode 转义，长度不可控；两个 dict
字段顺序不同但内容相同也 `repr` 出不同 key。

**规避**：用稳定的哈希作为 key 片段：

```python
import hashlib, json

def _cache_key(d: dict) -> str:
    sig = json.dumps(d, sort_keys=True, default=str)   # sort_keys 决定稳定性
    return hashlib.sha256(sig.encode()).hexdigest()[:16]
```

TTL 写短一点（120s），反正缓存内容下次重算成本低；写太长一旦业务改了字段定义，
旧 cache 会一直返回错的结果。

### 6.18 `asyncio.create_task` 在测试同步上下文里静默丢弃

**症状**：路由里 `asyncio.create_task(_cache_shapes_graph(...))` 把缓存写入
fire-and-forget，集成测试跑完后 Redis 里啥也没有，单元测试拿不到缓存结果。

**原因**：FastAPI lifespan 启动的事件循环里 `create_task` 才会被调度；测试里
ASGITransport 跑完同步退事件循环，未调度的 task 直接被 GC。

**规避**：

- 缓存写入走 `await _cache_shapes_graph(...)` — 业务等得起 5ms 网络 IO。
- 非要 fire-and-forget 的话，确保调用方还在事件循环里运行（FastAPI 路由里 OK，
  但单测要保留 loop 到 task 完成）。
- 或者把"写缓存"做成 best-effort 装饰器，捕获所有异常 + 记 WARNING。

### 6.19 Alembic autogenerate 误带无关字段

**症状**：`alembic revision --autogenerate` 生成的 migration 把全表所有列都
列了一遍（包括没改过的），review 时 diff 巨大、merge conflict 多。

**原因**：本地 SQLite schema 和生产 PG schema 元数据有微妙差异（enum 顺序、
index 顺序、CHECK 约束名等），autogenerate 会忠实地把它们全 dump 出来。

**规避**：

- autogenerate 是**起点**，跑完 diff 后人工审一遍，把无关行删掉。
- 写 `include_object` 过滤器只挑改了的表。
- 如果 diff 大到没法审，宁可手写迁移：加列就是 `op.add_column(...)` + index，
别让 autogenerate 全包。

### 6.20 字符串归一化时驼峰拆分必须在 `lower()` 之前

**症状**：`_normalize_key("userEmail")` 返回 `"useremail"` 而不是 `"user_email"`，
同义词表 / 字段名匹配全 miss。原本测试期望 "userEmail" → "user_email"。

**原因**：驼峰正则是 `([a-z])([A-Z])`，要求小写字母后面跟大写字母。如果先
`name.strip().lower()` 再 `re.sub(r"([a-z])([A-Z])", ...)`，所有大写都没了，正则
**永远不匹配**，驼峰边界完全识别不到。

**规避**：处理字段名 / 列名归一化时，统一用这个顺序：

```python
# 1. 先拆驼峰（在大小写都还在的时候）
s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name.strip())
# 2. 再统一分隔符（下划线 / 连字符）
s = re.sub(r"[_\-]+", "_", s)
# 3. 最后才 lower
return s.lower().strip("_")
```

`^[A-Z]+([A-Z][a-z])` 这类"连续大写后接小写"（如 `HTTPRequest` → `HTTP_Request`）也是
常见变体，如果业务里碰到可加 `r"([A-Z]+)([A-Z][a-z])"`。本仓目前只用 snake / camel /
kebab 三种，`re.sub(r"([a-z])([A-Z])", r"\1_\2", s)` 足够。

### 6.21 创建 ORM 对象后立刻用它做副作用 → 必须先 `flush()`

**症状**：路由 handler 里先 `decision = ReviewDecision(...)` 再执行一段后续逻辑
（写审计事件 / 更新其他表的 `confirmed_by`），后续逻辑里用 `decision.decided_by` /
`decision.created_at`，报 `NameError: name 'decision' is not defined`（变量在
else 分支后面才创建）或 `MissingGreenlet: ... lazy load ...`（flush 后属性过期）。

**原因**（两个独立 bug 都会撞）：

1. 变量作用域：代码块在变量定义**之前**就引用了它 — 顺序写反了。
2. ORM 默认 expire：仅 `session.add(obj)` 不 `flush()`，访问 `obj.id` /
   `obj.created_at` 会触发 lazy load，async session 里就是 `MissingGreenlet`。

**规避**：写"创建 A → 用 A 的字段做副作用"这种模式时，严格按这个顺序：

```python
# 1. 构造 ORM 对象
decision = ReviewDecision(
    proposal_id=proposal_id,
    decided_by=uuid.uuid4(),
    ...
)
session.add(decision)
# 2. 先 flush，让 DB 生成 id / created_at 填充到对象
await session.flush()
# 3. 这下 decision.id / decision.decided_by / decision.created_at 都可用
audit = AuditEvent(
    actor_id=decision.decided_by,
    ...
    target_id=str(decision.id),
    created_at=decision.created_at,
)
session.add(audit)
# 4. 临近返回前再 flush + refresh（可选）
await session.flush()
await session.refresh(decision)
```

**反向陷阱**：如果副作用链是 A → B（修改 B 引用 A），又 B → C，则中途失败要
回滚时，要把 A 也回滚；用 `Depends(get_session)` 走 FastAPI 默认事务边界就行，
handler `raise HTTPException` 会自动 rollback。不要自己 `async with session.begin()`
包一层 — 跟 `record_audit` 的二级 flush 兼容不好。

### 6.22 测试方法引用模块级 import 的 helper 时也要先 import

**症状**：在 `tests/test_xxx.py` 里的测试类 `TestXxx.test_yyy` 写
`from src.services.foo import _helper` 后，`test_zzz` 里直接 `assert _helper(...)` 报
`NameError: name '_helper' is not defined`。

**原因**：每次 test 方法内部的 import 只在该 method 作用域里生效，不会冒泡
到 test class 其它方法里。不 import 就用 → NameError。

**规避**：

- 模块顶部一次性 `from src.services.foo import _helper`（**推荐**，清晰且省时间）。
- 或每个用到的 test 方法里都加一行 `from src.services.foo import _helper`。
- 别抄 "前面那个测试里有 import 了" 的代码 — 那份 import 只对那个 method 有效。

### 6.23 Alembic `op.create_index(checkfirst=True)` 不存在 — `checkfirst` 只对 `op.create_table` 有效

**症状**：跑 `alembic upgrade head` 报
`TypeError: Additional arguments should be named <dialectname>_<argument>, got 'checkfirst'`。

**原因**：`op.create_index` 不接受 `checkfirst=True`。该参数是
`op.create_table` 的；index 在 SQLite 上也没有 `IF NOT EXISTS` 原生语法。

**规避**：自己包一层 helper：

```python
def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    insp = sqla_inspect(bind)
    return any(idx["name"] == index_name for idx in insp.get_indexes(table))


def _create_index_safe(name: str, table: str, columns: list[str], **kw) -> None:
    if _has_index(table, name):
        return
    op.create_index(name, table, columns, **kw)
```

### 6.24 给现有 PG enum 加值必须用 `ALTER TYPE ... ADD VALUE`，且在事务外执行

**症状**：要给现有 enum 加新值，`UPDATE` / `INSERT` 写新值时报
`InvalidTextRepresentation: invalid input value for enum ...`；或者
Alembic 跑迁移时报 `ALTER TYPE ... ADD cannot run inside a transaction block`。

**原因**：

- PG 12-：`ALTER TYPE ... ADD VALUE` 不能在事务块里跑（需要 implicit
  commit），而 Alembic 默认每个 migration 包一个事务。
- 改了 SQLAlchemy model 的 enum 不改 PG enum type，DB 端 CHECK 不到。

**规避**：

```python
if _is_postgres():
    bind = op.get_bind()
    with bind.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for new_value in ("submitted", "changes_requested", "closed"):
            conn.execute(sa.text(
                f"ALTER TYPE change_request_status "
                f"ADD VALUE IF NOT EXISTS '{new_value}'"
            ))
```

- **PG 9.6+** 才支持 `IF NOT EXISTS`，老版本需要先查 `pg_enum` 决定要不要加。
- **降级不要尝试 `ALTER TYPE DROP VALUE`**（PG 12- 不允许事务内执行；
  12+ 允许但会强制 cascade，丢历史数据）。保留扩展值无害 — 应用层不再写就行。
- SQLite 上 enum 是字符串 + CHECK 约束；改 Python enum 后 `Base.metadata.create_all`
  不会重建表，需要 drop_column + add_column 才能改 CHECK。**建议在 SQLite 上不依赖 enum 约束**，
  让 Python 端做校验。

### 6.25 CR 状态机：`SUBMITTED → APPROVED` 不能由「全部 reviewer 都投票」一步触发

**症状**：写多 reviewer 自动合并时，把所有 reviewer approve 的逻辑直接
塞在 `POST /change-requests/{id}/approve`（全局 approve）端点里 —
配了多 reviewer 的 CR 也走这条路，结果第二个 reviewer 投了之后又走一
遍「全局 approve」，状态正确但 `approved_by` 被覆盖成 admin，不是
reviewer 自己。

**原因**：混淆了「全局 approve」（无 reviewer 配置时一键通过）和
「per-reviewer approve」（多 reviewer 工作流中的单人投票）。两套路径
互相覆盖了对方的副作用。

**规避**：

- `POST /change-requests/{id}/approve` — 保留给 `required_approvers == 0`
  或「无 reviewer 配置」场景；多 reviewer 时返回 400 引导客户端走另一
  个端点。
- `POST /change-requests/{id}/reviewers/{rid}/approve` — per-reviewer
  投票。每次调用：
  1. 把该 reviewer 状态置 `APPROVED`，写 `reviewed_at`。
  2. 调 `_maybe_auto_merge(cr, session)`：如果「APPROVED 计数 >=
     required_approvers」就把 CR 升级到 `APPROVED`，**并把 `approved_by`
     记成「最后那个把 CR 推过线」的 reviewer**（避免覆盖）。
- 重提交流程：作者改完点 re-submit → CR 从 `CHANGES_REQUESTED` 回到
  `SUBMITTED`，**所有 reviewer 状态重置为 `PENDING`**，否则上一次的
  `APPROVED` 票会让 `_approved_reviewer_count` 立刻满足 → 跳过 review。
- 状态机边界：merge / close 是终态，不能从 `MERGED` / `CLOSED` 继续
  approve / reject；端点层必须校验当前状态。

### 6.26 SQLAlchemy relationship `back_populates` 配 self-referential 模型（评论线程）容易循环

**症状**：写 threaded comment 时
`ChangeRequestComment.replies = relationship("ChangeRequestComment", back_populates="comment")`，
启动报 `InvalidRequestError: Mapper ... has no property 'comment'` 或
死循环 import。

**原因**：self-referential 模型加 `back_populates` 必须两边都写，且父
端加 `remote_side=[Column]`；不然 SQLAlchemy 没法判定谁是父谁是子。

**规避**：adjacency-list threaded comment 通常**只配单向 relationship** —
`replies = relationship("ChangeRequestComment", cascade="all, delete-orphan")`
但**不**加 `back_populates`；查询时显式 `WHERE parent_id == ?`。本仓
`ChangeRequestComment.replies` 就是这么写的。

如果一定要双向，加：

```python
replies: Mapped[List["ChangeRequestComment"]] = relationship(
    "ChangeRequestComment",
    back_populates="parent",
    remote_side=[id],  # 父端
    cascade="all, delete-orphan",
)
parent: Mapped["ChangeRequestComment"]] = relationship(
    "ChangeRequestComment",
    back_populates="replies",
    remote_side=[ChangeRequestComment.parent_id],
)
```

### 6.27 PG connector 写库必须"白化"连接信息（HIA-67）

**症状**：`snapshot-to-evidence` 端点把 connector 的 `config` dict 直接写到
`sources.connection_info`，结果：
1. **密码泄漏到 DB** — 加密只在 `connectors.config` 字段有效；`sources.connection_info`
   是普通 JSON，没人解密它，等于明文。
2. **`/projects/{id}/sources` 接口返回带明文密码的对象** — 任何项目成员都看得到。
3. **源标识错位** — 后端再用 `sources.connection_info` 重连 PG 会拿错凭据。

**原因**：直接复制原始 config 是最省事的实现路径，但忽略了"两边字段的
加密策略不同"。

**规避**：snapshot-to-evidence 端点**只写白化字段**：

```python
safe_conn_info = {
    "type": "postgresql",
    "host": plain_cfg["host"],
    "port": plain_cfg.get("port", 5432),
    "database": plain_cfg["database"],
    "schema": plain_cfg.get("schema_filter", []),
    "table_filter": plain_cfg.get("table_filter", []),
    # 显式不写 password / username
}
# 关联回原 connector 用于追溯
schema_info = {"table": body.table, "connector_id": str(conn.id)}
```

如果一定要保留完整凭据（不可取），至少用 `src.core.secrets.encrypt_secret_fields`
先加密再写 `connection_info`；但更干净的做法是**不存**，靠 `connector_id` +
`connector.secret_fields` 的加密链路管理凭据。

### 6.28 SQLAlchemy JSON 字段 in-place 修改在 SQLite 上不会被检测（HIA-61）

**症状**：

```python
# 错误写法：看起来对，跑测试也有 commit，但 artifacts['tags'] 永远是 []
if field == "tags":
    if release.artifacts is None:
        release.artifacts = {}
    release.artifacts["tags"] = value  # ← 这一行 SQLAlchemy 没看到变更
await session.flush()
await session.refresh(release)
```

PATCH 返回的 `artifacts.tags` 是 `[]`，不是新写入的 `["updated"]`。

**原因**：SQLAlchemy 默认不"深度监听" JSON 列。`Mapped[dict] = mapped_column(JSON)`
没有用 `MutableDict.as_mutable(JSON)` 包装，所以 `dict["key"] = value` 这种
in-place 改动**不会触发 ORM 的 dirty 检查**；只有 `release.artifacts = new_dict`
这种属性重新赋值才会被检测。

SQLite + aiosqlite 表现更明显：commit/flush 不会报"未保存的修改"，但实际写入
的是 `{}`（in-place 之前的快照），然后 `session.refresh()` 再覆盖回来，最终
什么也没存。

**规避**：JSON 字段**任何修改都必须整体赋值**：

```python
# ✅ 正确：合并 + 整体赋值
if field == "tags":
    current = release.artifacts or {}
    release.artifacts = {**current, "tags": value}
```

或者用 SQLAlchemy 提供的 `MutableDict.as_mutable(JSON)` 在模型层注册监听，但
这是全局改造，所有现存 JSON 字段都要审一遍；不推荐中途切换。

**检验方式**：跑一个 `test_update_release` 之类的 happy-path，写 JSON 字段后
再 `await session.refresh(obj)`，断言字段值已变更。如果仍是旧值，立刻怀疑
in-place 修改。

### 6.29 `selectinload(SomeModel.missing_relationship)` 不会编译失败（HIA-61）

**症状**：

```python
query = select(Deployment).options(selectinload(Deployment.release))
```

启动时 `import` 不报错，`pytest` 跑起来也不报 `KeyError`，但**第一次请求命中
这个查询**就抛：

```
AttributeError: type object 'Deployment' has no attribute 'release'.
Did you mean: 'release_id'?
```

**原因**：SQLAlchemy 的 `selectinload(SomeModel.relationship)` 是字符串式访问
类属性，只有在**实际构造查询**（即请求第一次进来）时才解析 relationship 名。
如果是 relationship 名根本不存在，Python 是解释型访问，直接抛 AttributeError
而不是导入期错误。

**规避**：
1. 写端点前**先确认 ORM 模型上的 relationship 名**；只有显式声明
   `relationship("Release", back_populates="...")` 才有 `Deployment.release`。
2. 没有 relationship 但要 join 用 `select(...).join(Release).where(...)` 就够了，
   **不需要 `options(selectinload(...))`**。
3. 写完端点后**真实启动 + curl 一次**触发懒加载路径，仅靠单元测试查不到
   （如果 selectinload 永远走不到、或者测试覆盖不全）。
4. 旧项目里如果发现历史代码写了 `selectinload(Model.x)` 但 `Model.x` 不存在，
   用 grep 一次性清理：

```bash
rg "selectinload\(([A-Z][A-Za-z]+)\.([a-z_]+)\)" -or '$1.$2' | sort -u
```

然后逐个去模型里核对。

### 6.30 数据库 NOT NULL 字段必须出现在 Pydantic Create schema（HIA-61）

**症状**：

```python
class UseCaseBundleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    version: str
    # 漏了 release_id

class UseCaseBundle(Base):
    # ORM 模型
    release_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey(...), nullable=False)
```

POST 端点接收合法 JSON 后，跑到 `session.add(bundle); await session.flush()` 时
才抛 `IntegrityError: NOT NULL constraint failed: use_case_bundles.release_id`。
开发体验差，且容易在生产触发 500。

**原因**：Pydantic schema 没有对应字段 → `data.release_id` 是 `None` → ORM
构造时收到 None → DB 拒绝写入。模型和 schema 是两套独立的"接口契约"，缺一
就崩。

**规避**：
1. 任何 `nullable=False` 的 ORM 字段，**对应的 *Create schema 字段必填，
   *Update schema 字段可选**。
2. 端点处理函数**第一行**就校验外键存在：
   ```python
   if data.release_id:
       await _verify_release_exists(session, data.release_id)
   ```
3. 加集成测试覆盖"完整正常 body"的 happy-path；只测异常 case 测不出
   schema 缺字段。
4. 自动化 lint（可选）：扫 ORM `nullable=False` 字段，对比同名 schema 类
   是否包含。dev 阶段不做也行，但建议把这一条加进 PR review checklist。

### 6.31 测试文件在模块级 `from src.db.connection import async_session_factory` 会绑定 stale 单例

**症状**：每个 test 函数拿到一个**全新的临时 SQLite DB**，但 helper 函数读
写操作仍然命中上一次（或默认 PG）的 session；表现是「fixture 看起来生效了，
但实际写到 `tmp.db` 之外的某个 DB」「跨测试的 isolation 完全没起作用」「某个
test 改了数据，**下一个 test 看到的却是旧值**」。

**原因**（与 §6.8 / §6.22 不同的另一类陷阱）：

- `src.db.connection.async_session_factory` 是**模块级单例**（构造时绑定
  `async_engine`，再从 engine 拉 URL）。
- 测试文件如果顶端写了
  `from src.db.connection import async_session_factory, reinit_engines`，
  Python 在 import 时就把这个全局名**绑定到当时指向的那个对象**。
- fixture 里再 `await conn.reinit_engines()` 会让 `conn.async_session_factory`
  重新指向新对象，但测试模块顶部那个本地名 `async_session_factory` 还指向旧
  对象。
- helper / 测试方法里再用这个局部名 → 操作旧 DB（通常就是 .env 里的 PG 或默认
  的同进程 sqlite）。fixture 白跑。

**规避**：

- 模块级只放占位：`async_session_factory = None  # type: ignore[assignment]`，
  fixture 内部 `await conn.reinit_engines()` 之后做
  `async_session_factory = conn.async_session_factory`（用 `global` 声明）。
- 或者**完全不引用模块级名**：helper 函数内部都通过
  `from src.db import connection as conn; ... conn.async_session_factory() ...`
  这种「每次现场拉」的方式写。
- 模板见 §4.1 `isolated_app` 标准 fixture；**直接复制**那段代码，不要自己
  拼 `from src.db.connection import async_session_factory`。
- 校验：fixture 跑完之后立刻
  `assert async_session_factory is not None and async_session_factory is conn.async_session_factory`，
  不一致就立刻报错。

**反例**（不要照着抄）：

```python
# ❌ import 时绑定，reinit 之后这个名还指向旧对象
from src.db.connection import async_session_factory, reinit_engines

@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    from src.core import config as cfg; cfg.get_settings.cache_clear()
    await reinit_engines()
    # 此时 conn.async_session_factory 已切到 tmp.db，
    # 但本模块的 async_session_factory 局部名仍指向旧对象 → bug
```

### 6.32 编辑工具误删 `<Task X>` 行而没补全

**症状**：`docs/MILESTONES.md` / `docs/DEVELOPMENT.md` 的章节列表里突然少了一节，
例如 §6 突然从 §6.29 跳到 §6.31（其实中间 §6.30 还活着，只是被吞掉了）；或
者某个 PR 的标题里 `[Task X]` 前缀消失、CI 没拦截，merge 后追溯不到对应卡。

**原因**：在文件里做精细改动时，编辑工具（IDE / 脚本 / 手工 patch）按行号
匹配上下文；若 anchor 行恰好紧贴 `<Task XXX>` 或 `<section Y>` 这类「意图」
行（例如「新增 §6.31」后面那行就是 `<Task XXX>`），diff 一晃容易把上一行
一并删掉。Python / Markdown 的纯文本编辑不像 word 那样有「撤销整段」概念，
工具可能直接落盘成「§6.30 + §6.32」中间一节被吞。

**规避**：

- 改文件前先 `git diff` 一次 baseline，看清原 anchor 上下文；改完再 diff 一
  次，发现吞行立刻 revert + 重做。
- 一次只改一个最小单元：新增 §X.Y 单独一 commit，单独一个 diff 块；不要把
  「新增 §6.31 + 修改 §4.1 + 新增 §6.32」塞进同一个 patch。
- §6 / §MILESTONES 这种线性编号文档，新增条目时**手抄上一条 + 下一条的标题
  作为锚点**（双 anchor），避免编辑工具按"上一条内容"删半截。
- CI 不强制编号连续；偶尔跳号没事，发现了再补一行说明。

### 6.33 E2E 走通测试中发现的 API 契约偏差（HIA-66）

E2E demo 测试（`tests/test_e2e_demo.py`）完整跑一遍产品主链路，发现了
多个「schema 定义 ↔ 实际 API 行为」不匹配的问题，都是单元测试没覆盖到的。

#### 6.33.1 `POST /releases/{id}/preflight` body 必须传 `environment` 字段

**症状**：测试里直接 `POST /releases/releases/{id}/preflight` 不带 body，返回
`422 {"detail":"Field required"}`。

**原因**：`PreflightRunRequest` 定义了 `environment: str = Field(...)`，
FastAPI 要求 body 里有这个字段（没有默认值）。

**规避**：调 preflight 端点时，带 `json={"environment": "local"}`
（或其他环境）作为 body；未来如果要支持无 body 调用，
`environment` 需改成 `Optional[str] = Field(default="production")`。

#### 6.33.2 `POST /releases/projects/{id}/deployments` — 不是 `/deployments/releases/{id}/deploy`

**症状**：调用 `/deployments/releases/{release_id}/deploy` 返回 404。

**原因**：部署路由是 `POST /projects/{project_id}/deployments`（在 release.py 里），
request body 是 `DeploymentCreate`，包含 `release_id`、`environment` 等字段。

**规避**：部署调用方式：
```python
r = await client.post(
    f"/releases/projects/{project_id}/deployments",   # 注意：是 projects/{project_id}/deployments
    json={"release_id": release_id, "environment": "local"},
)
```

#### 6.33.3 `POST /objects/projects/{id}/objects` — `object_type` 必须是 enum 值，`name` 必填

**症状**：传 `object_type: "Customer"`（自由字符串）和空 body 返回 422。

**原因**：`ObjectCreate.object_type` 是 `ObjectType` 枚举，合法值只有
`entity | event | activity | agent | place | document | other`；且 `name` 字段是必填的。

**规避**：创建对象时：
```python
r = await client.post(
    f"/objects/projects/{project_id}/objects",
    json={
        "object_type": "entity",   # 枚举值，不是自由字符串
        "name": "Alice Chen",      # name 必填
        "data": {"email": "alice@acme.test"},
    },
)
```

#### 6.33.4 `POST /validation/runs` — `project_id` 是 Query 参数，body 用 `target_type`/`target_id` 结构

**症状**：用 `POST /validation/projects/{id}/runs`（path）返回 404；
用 `POST /validation/runs` 传 `validation_type` 但模型字段不匹配抛 AttributeError。

**原因**：验证运行创建端点的 `project_id` 是 Query 参数，不是路径参数；
且 `ValidationRun` 模型用 `target_type`/`target_id`/`name` 字段，
`ValidationRunCreate` 的字段名和模型字段名有映射关系。

**规避**：创建验证运行的正确方式：
```python
r = await client.post(
    "/validation/runs",
    params={"project_id": project_id},    # Query 参数，不是 path
    json={
        "validation_type": "shacl",      # → 映射到 ValidationRun.name
        "ontology_version_id": v2_id,    # → 映射到 target_type="ontology" + target_id
        # 或 mapping_version_id 用于 mapping 类型
    },
)
```

#### 6.33.5 `execute_validation_run` — `ValidationStatus` 没有 `SKIPPED` 枚举值

**症状**：`execute_validation_run` 里 `run.status = ValidationStatus.SKIPPED`
抛 `AttributeError: 'ValidationStatus' has no attribute 'SKIPPED'`。

**原因**：`ValidationStatus` 只有 `PENDING | RUNNING | PASSED | WARNING | FAILED | ERROR`。
`WARNING` 用来表示"跳过了实际执行"的状态。

**规避**：不要用 `SKIPPED`；用 `WARNING` 替代。

#### 6.34 `from x import func` 让 `monkeypatch.setattr` 失效（HIA-79 D2）

**症状**：测试用 `monkeypatch.setattr(sso_client_mod, "fetch_discovery", fake)`
替换模块属性，运行时仍然调到原版（跑出去打真实 HTTP，CI 报 `ConnectError`）。

**根因**：`sso.py` 的 `from src.services.sso_client import fetch_discovery` 把
函数对象**绑定到 `sso.py` 模块自己的命名空间**。后续 `monkeypatch.setattr`
只改的是 `sso_client_mod` 模块的属性，`sso.py` 那份绑定不受影响。

**修复模板**：在被测代码里改成"导入模块、走模块属性访问"：

```python
# 错：monkeypatch 失效
from src.services.sso_client import fetch_discovery, exchange_code_for_tokens
await fetch_discovery(...)

# 对：测试可以 stub
from src.services import sso_client as sso_client_mod
await sso_client_mod.fetch_discovery(...)
```

测试继续按原方式 patch：

```python
monkeypatch.setattr(sso_client_mod, "fetch_discovery", _fake_fetch_discovery)
```

**何时该用**：（a）外部 IO 的 stub（HTTP/SMTP/DB/SDK），（b）需要替成
raise 触发错误路径，（c）需要断言是否被调用。**何时不用**：纯 helper
（PKCE / URL builder / claim 提取）——这些测试直接 import 调用就好。

#### 6.35 SQLite DateTime 列丢失 tz info — 比较前先 normalize（HIA-79 D2）

**症状**：模型声明 `expires_at: Mapped[datetime] = mapped_column(
DateTime(timezone=True), ...)`，写入 `datetime.now(timezone.utc)`，读出
后 `self.expires_at < datetime.now(timezone.utc)` 抛
`TypeError: can't compare offset-naive and offset-aware datetimes`。

**根因**：SQLite 没有原生 timestamp with timezone，写入时 SQLite 层把
tzinfo 剥掉；读出来是 naive datetime。PostgreSQL 不会这样，所以本地
SQLite 测试通过、上 PG 才暴露，或反过来。

**修复模板**：所有"时间比较"都通过一个 helper 走：

```python
@property
def is_expired(self) -> bool:
    from datetime import datetime, timezone
    exp = self.expires_at
    if exp.tzinfo is None:                          # SQLite 把 tz 丢了
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < datetime.now(timezone.utc)
```

同样的模式适用于所有"`expires_at` / `valid_until` / `not_before`"类的
业务时间字段。如果跨 dialect 都要正确，统一在 ORM 层 normalize 比每个
调用方各自处理更稳。

#### 6.36 `async_session_factory()` 不自动 commit（HIA-79 D2）

**症状**：测试 fixture 里
```python
async with async_session_factory() as s:
    row = (await s.execute(select(...))).scalar_one()
    row.expires_at = past_datetime
    await s.flush()                              # 只 flush 没 commit
    state = row.state

# 接着打下一个请求，期望读到修改后的值
r = await client.get(f"/api/sso/callback?...&state={state}")
assert r.status_code == 400                     # 实际返回 302
```

**根因**：`async with async_session_factory()` 退出时**不会自动 commit**。
`flush()` 只把改动推到 connection 缓冲；事务没 commit，关闭时就回滚到
上一次 commit 之后的快照。下一个请求看到的是修改前的值。

**修复**：fixture 里手动 commit：

```python
async with async_session_factory() as s:
    row = (await s.execute(...)).scalar_one()
    row.expires_at = past_datetime
    await s.flush()
    await s.commit()                            # ← 必须
    state = row.state
```

**对比**：FastAPI `Depends(get_session)` 会自动 commit（见
`src/db/connection.py`），所以走 HTTP 路径的代码不用担心；后台 task /
测试 / 脚本里自己开 session 的代码路径必须显式 commit。这条与
§25.8（workflow executor owned-session）是同一根因的不同表现。

#### 6.37 加密字段的"加密别名"会让测试断在奇怪的格子（HIA-79 D2）

**症状**：测试期望 `body["config"]["client_secret"] == "***"`，但实际返
回明文；又期望数据库里 `enc:v1:` 前缀存在，但实际什么前缀都没有。

**根因**：默认 `secret_fields=["client_secret_enc"]`（"加密字段别名"），
但 API 接收/返回的是 `client_secret`。结果：
- 写入时：`config["client_secret"]` 是明文，`config["client_secret_enc"]`
  不存在 → 没东西被加密；
- 读取时：mask 在 `client_secret_enc` 上做 → `client_secret` 仍是明文。

**最佳实践**：加密的字段名 = 用户写的字段名 = OIDC/SAML/LDAP 规范字段名。
OIDC spec 的字段就叫 `client_secret`，不要硬造 `client_secret_enc` 别名
——加一层名字混淆只会让 API/DB/前端 三方契约对不齐。

```python
# 错：别名导致 mask 和 encrypt 命中不同 key
DEFAULT_OIDC_SECRET_FIELDS = ["client_secret_enc"]

# 对：和 OIDC spec 字段名一致
DEFAULT_OIDC_SECRET_FIELDS = ["client_secret"]
```

如果将来真的需要"明文 vs 加密"区分，靠字段值的 `enc:v1:` 前缀判别
（`encrypt_value` 已经这样做了），不要再造一层字段别名。

#### 6.38 open-redirect 防御要在 login 入口就 sanitize，不能只信 callback（HIA-79 D2）

**症状**：`return_to=https://attacker.example/steal` 进到 login 接口；
login 接口只把 `return_to` 原样存到 `SsoLoginSession.relay_state`；callback
接口虽然会 `_validate_return_to` 替换为 `/`，但 `relay_state` 列里已经
留了攻击者 URL，DB dump / 审计就能看到这个 URL。

**修复**：在 login 入口就过一次 sanitizer：

```python
safe_relay_state = _validate_return_to(return_to)
sso_session = SsoLoginSession(
    ...
    relay_state=safe_relay_state,
    ...
)
```

**原则**：任何"用户输入且会回显/落盘"的字段，sanitize 在**第一个**入
口做，而不是依赖"后续每个使用点都会过滤"。后者每加一个 caller 就多
一个漏点，前者一处搞定。

#### 6.33.6 写 E2E 测试时：先走通，再打磨

**经验**：写 E2E 测试的过程中发现了 4 个 API 契约 bug
（见 §6.33.1–6.33.5），这些问题在单元测试里没有覆盖，
因为单元测试用的是 mock/in-memory 数据，绕过了真实的 API 路由解析和 schema 校验。

**教训**：
1. **每个新端点**写一个走 ASGI transport 的集成测试（至少调一次完整 HTTP 往返）。
2. 测试前先看 `ValidationRunCreate` 等 schema 的实际字段定义，不要按「看起来合理」
   的字段名写请求。
3. 发现 422 时，把 `r.text` 完整打印出来；FastAPI 的 `detail` 字段会准确告诉
   你缺了什么字段和类型不匹配的原因。

#### 6.39 `record_audit(principal=...)` 同时被两种 caller 调用 — duck-type 一下（HIA-90 D5）

**症状**：从 HIA-77 D1 开始，admin-only 端点用
`user: User = Depends(require_global_admin)` 拿到的是 `User`，但 `record_audit`
的签名是 `principal: CurrentPrincipal`，第一行就是 `actor = principal.user`，
运行时报 `'User' object has no attribute 'user'`。

**根因**：项目级端点（`webhooks.py` / `workflow.py` / `release_line.py` / `objects.py`）
用 `Depends(get_current_user)` 拿到 `CurrentPrincipal`，传 `principal=principal`；
admin-only 端点（`backup.py` / 后续的 `auth_admin.py` 风格的依赖）直接返
`User`，传 `principal=user`。两种传法一直并存，但 `record_audit` 内部只
认 `CurrentPrincipal`。

**修复**：`record_audit` 一开始 duck-type 一下，accept `Union[CurrentPrincipal, User]`：

```python
async def record_audit(session, *, principal, ...):
    if isinstance(principal, CurrentPrincipal):
        actor = principal.user
    else:
        actor = principal  # User 自身
    ...
```

不要去改每个 caller — 那会推动所有 admin endpoint 改返回类型，
影响面比改 `record_audit` 大得多。`require_global_admin` 故意返 `User`
是为了让 caller 拿到 `.email` / `.global_role` 这种不需要通过 principal
间接访问的属性。

#### 6.40 pytest fixture 里用 env-var 调配置，但 manager 没看到（HIA-90 D5）

**症状**：`isolated_app` fixture 已经
`monkeypatch.setenv("BACKUP_STORAGE_DIR", str(storage))`，但下游
`manager` fixture 直接构造 `BackupManager(storage_dir=isolated_app["storage"])`
时，manager 内部走的 `settings.backup.storage_dir` 还是 module-import
时的旧值。

**根因**：`BackupManager` 内部从 `settings.backup.storage_dir` 读路径，
而 `settings` 是 module-level singleton，启动时通过 `get_settings()`
加载。`monkeypatch.setenv` 只影响**新一次** `get_settings()` 调用 —
但 settings 已经缓存了。

**修复**：在 `manager` fixture 里：

1. `cfg.get_settings.cache_clear()` 强制重读。
2. `monkeypatch.setattr(cfg.settings.backup, "storage_dir", ...)` 直接改
   缓存好的 settings 对象。

```python
@pytest_asyncio.fixture
async def manager(isolated_app, monkeypatch):
    from src.services.backup import BackupManager
    from src.core import config as cfg

    cfg.get_settings.cache_clear()
    monkeypatch.setattr(cfg.settings.backup, "storage_dir", str(...))
    monkeypatch.setattr(cfg.settings.backup, "work_dir", str(...))
    # ... 所有 manager 会读到的字段
    return BackupManager(storage_dir=..., work_dir=..., encryption_key="")
```

教训：把"读 env"和"读 settings attribute"分开对待。`monkeypatch.setenv`
只覆盖前者；后者要直接 `setattr` 到 settings 对象。

#### 6.41 加密备份里 `manifest.json` 必须**先写**再 tar（HIA-90 D5）

**症状**：encrypted backup 创建成功，但 `restore_backup(dry_run=True)`
立刻抛 `BackupRestoreError("no components to restore")`。

**根因**：原来的流程是：

```python
# 1) 备份各 component
manifest["components"][name] = comp.to_dict()

# 2) 把整个 backup_dir tar 成 plain.tar，再加密成 backup.enc
await self._pack_and_encrypt(backup_dir, component_objs)

# 3) 在 backup_dir 下写 manifest.json  ← 太晚了！
(backup_dir / "manifest.json").write_text(json.dumps(manifest))
```

encrypted 路径下，`manifest.json` 是 step 3 才写的，**不在** step 2 加密的 tarball 里。
restore 时解密到 `staging/backup/manifest.json` — 不存在 → manifest=None
→ `components = list(manifest.get("components", {}).keys())` 不执行 →
`if not components: raise`。

**修复**：写 manifest 必须在 `_pack_and_encrypt` **之前**。plain (unencrypted)
模式无所谓 — manifest.json 留在 backup_dir 里给 `verify_backup` 用。encrypted
模式把 manifest 跟着 tarball 一起加密，restore 时从 staging 解出来读。

#### 6.42 staging 目录名用 `int(time.time())` 在快速重跑时冲突（HIA-90 D5）

**症状**：`restore_backup` 用
`staging = work_dir / f"restore_{backup_id}_{int(time.time())}"` 然后
`staging.mkdir(parents=True, exist_ok=False)` — 同一秒内调用两次时
第二次 `mkdir` 抛 `FileExistsError`。

**根因**：测试里 DR drill 在 restore 之后又做一次 restore，时间戳秒级
精度撞上。生产中也可能：同一 backup id 快速触发多次健康检查。

**修复**：用微秒精度 + uuid 后缀：

```python
staging = self.work_dir / (
    f"restore_{backup_id}_"
    f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_"
    f"{uuid.uuid4().hex[:8]}"
)
```

教训：磁盘路径里只要涉及 `mkdir(exist_ok=False)` / `open(mode='x')`
/ `mkstemp`，timestamp 至少给到微秒 + 随机后缀。

---

## 7. 工具链 / 环境陷阱

### 7.1 Windows 路径里的 `\` 与 PowerShell 引号

- PowerShell 里嵌套引号转义很难看；优先用 `python -c "..."` 或写临时 .py 文件再 `python script.py`。
- 用 `Get-Content` 读 UTF-8 文件偶尔丢字符；读代码统一用编辑器 Read。

### 7.2 venv 在 Windows 上路径空格

- 默认装在 `D:\qiushi\ontoloHub\venv` OK；如果装到 `C:\Program Files\...` 之类的带空格路径，`pip install` 部分包会失败。一律放在仓库根。

### 7.3 前端 dev server 起在 3000，但 vite proxy 指向 8000/8001

- 后端默认 `API_PORT=8000`；`apps/web/vite.config.ts` 的 proxy 要和 `.env` 里 `API_PORT` 对齐。改了 `.env` 要重启 vite。

### 7.4 Alembic 在 SQLite 上跑 `op.create_index` 失败

- 早期 SQLite 不支持并发事务里的 DDL；多 statement 迁移会被 silently rollback。
- 规避：复杂迁移拆成多个 revision，每个 revision 内部自己管 transaction。

### 7.5 `uvicorn --reload` 在 Windows 上不监听到改动

- 偶尔发生；`pip install watchdog` 装上 watchfiles 后 `--reload` 才会稳定工作。

---

## 8. 评审清单（PR Reviewer 视角）

每个 PR 至少要回答这几个问题：

- [ ] 对应的 Linear 卡链接？
- [ ] 新端点是否挂了 `require_role` / `require_role_query`？
- [ ] 跨项目隔离负例覆盖了吗？404 而不是 403 吗？
- [ ] 写端点都调了 `record_audit` 吗？审计链 `verify` 通过吗？
- [ ] Alembic 迁移存在且 `upgrade / downgrade` 对称？
- [ ] 新代码加了测试？happy / 跨项目 / 角色不足 / 审计 / 入参校验 五类都有？
- [ ] 没有 hard-coded URL / 端口 / 密钥？
- [ ] 新增的 connector 类型写了单元测试？测试了 offset/limit 截断吗？
- [ ] Connector config 的 `secret_fields` 字段在落盘前加密了吗？测试验证 DB 里是密文吗？
- [ ] 新加 Redis 缓存的代码有 `try/except CacheUnavailable` 降级路径吗？
- [ ] Redis 测试用 fakeredis，没连真 Redis？
- [ ] 没把 `datetime.utcnow()` / `func.now()` 用在审计时间上？
- [ ] 没把 ORM 实例直接当 Pydantic 返回？
- [ ] 改动没破坏 §1–§5 任何一条？

---

## 9. Connector 框架（HIA-71 / HIA-67）

### 9.1 架构

```
src.services.connectors/
    _framework.py   # 基类 + 注册表 + 数据结构（**不要** 从 __init__ 倒导）
    __init__.py     # re-export + import 触发 @register
    file_connectors.py   # CSV / Excel / JSON / Parquet
    postgres_connector.py # PostgreSQL 只读
```

**导入顺序规则**：`_framework.py` 定义所有类；`__init__.py` 从 `_framework` re-export；
**禁止** 在 `_framework.py` 里 `from .__init__ import ...`（循环导入）。

### 9.2 新增 connector 类型

```python
# 在对应的 _connectors.py 里：
@register
class MyConnector(Connector):
    type: ClassVar[str] = "my_type"   # 注册 key，全局唯一

    async def test_connection(self) -> tuple[bool, str]: ...
    async def list_tables(self) -> list[TableInfo]: ...
    async def snapshot(self, table, *, limit=1000, offset=0) -> SnapshotResult: ...
```

在 `__init__.py` 底部 `import` 触发装饰器生效。

### 9.3 敏感字段加密

`Connector.config` 中任何包含密码/token 的字段必须在 `secret_fields` 列表里声明，
API 会在落盘前调用 `encrypt_secret_fields(config, secret_fields)`，读取时
`mask_secret_fields` 替换为 `"***"`。

**永远不要**把明文密码写到 `config` 里再存 DB。

```python
# API 层示例（见 apps/api/src/api/connectors.py）
from src.core.secrets import encrypt_secret_fields, decrypt_secret_fields, mask_secret_fields

# 创建
encrypted = encrypt_secret_fields(raw_config, ["password", "token"])
conn = Connector(config=encrypted, secret_fields=["password", "token"], ...)

# 读取（默认 mask）
cfg = mask_secret_fields(conn.config, conn.secret_fields)  # → {"password": "***"}

# 读取明文（需要 EDITOR+）
cfg = decrypt_secret_fields(conn.config, conn.secret_fields)
```

### 9.4 API 路由模式

Connector API 遵循 `project_id` 放 **Query 参数**的模式（与其他 Sources/Evidence 路由一致），
所以用 `require_role_query`。

## 10. Redis 缓存（HIA-64 B1）

### 10.1 模块

`src.core.cache` 提供全局 `Cache` 单例：

- `await get_cache()` — 懒加载，连接池内置。
- `await cache.ping()` — 检查健康状态，失败不抛。
- `await cache.set(ns, key, value, ttl=60)` / `await cache.get(ns, key)` — 字符串读写。
- `await cache.set_json(ns, key, obj, ttl=60)` / `await cache.get_json(ns, key)` — JSON 包装。
- `await cache.incr(ns, key, ttl=60)` — 用于限流计数。
- `await cache.delete(ns, key)` — 删除。
- `await cache.aclose()` — 关闭连接池（lifespan 关闭时调用）。

所有 key 自动拼成 `<key_prefix>:<namespace>:<key>`，多服务共用 Redis 时不串。

### 10.2 不可用时不阻断

Redis 连不上 / 鉴权失败时 `Cache` 进入「degraded」模式：

- `get` / `set` / `incr` / `delete` 抛 `CacheUnavailable` — 调用方必须 try/except 降级。
- `ping()` 返回 `False` 不抛。
- `is_healthy` 属性始终反映最近一次 ping 结果。

启动时若 ping 失败，应用继续运行（记 WARNING），业务逻辑 fallback 到「无缓存」。
**不要**因为 Redis 故障把整条请求打 500。

```python
from src.core.cache import get_cache, CacheUnavailable

cache = await get_cache()
try:
    cached = await cache.get_json("mapping", key)
    if cached is not None:
        return cached
except CacheUnavailable:
    pass  # Redis 挂了 — 直接走原始计算路径

result = await compute_mapping(...)
try:
    await cache.set_json("mapping", key, result, ttl=300)
except CacheUnavailable:
    pass
return result
```

### 10.3 测试模式

不要让测试连真 Redis。monkeypatch `Cache._build_pool` 让其返回 `fakeredis.FakeRedis`：

```python
import fakeredis.aioredis as fakeredis_aioredis
from src.core import cache as cache_mod
from src.core.cache import Cache

@pytest_asyncio.fixture
async def fake_cache(monkeypatch):
    fake_client = fakeredis_aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(Cache, "_build_pool", lambda self: setattr(self, "_client", fake_client))
    await cache_mod.reset_cache()
    yield fake_client
    await cache_mod.reset_cache()
```

### 10.4 命名规范

`namespace` 用业务名，如：

- `mapping:<project_id>:<mapping_version_id>` — 映射缓存
- `shacl:<project_id>:<ontology_version_id>` — SHACL 报告缓存
- `rate:<user_id>:<endpoint>` — 限流计数
- `idempotency:<key>` — 幂等键

**不要**把 `project_id` 当 namespace 单独抽出来；多服务共用 Redis 时可能撞前缀。

### 10.5 配置

`.env` / 容器环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接 URL |
| `REDIS_ENABLED` | `true` | 设 `false` 强制降级（用于本地不启 Redis） |
| `REDIS_SOCKET_TIMEOUT` | `2.0` | 读写 socket 超时（秒） |
| `REDIS_CONNECT_TIMEOUT` | `2.0` | 连接握手超时（秒） |
| `REDIS_KEY_PREFIX` | `ontolohub` | 全局前缀，多服务防串 |

### 10.6 缓存 wiring 模板

把 Redis 缓存接入已有 service 时，推荐三段式：

```python
# src/services/<domain>.py

import hashlib, json
from src.core.cache import get_cache, CacheUnavailable

_TTL_PROFILE = 120      # 短：业务字段定义可能变
_TTL_LIST = 300         # 中：列表 / 聚合结果
_TTL_HEAVY = 1800       # 长：expensive 解析（SHACL shapes 等）

async def _cache_get_json(ns: str, key: str):
    try:
        cache = await get_cache()
        return await cache.get_json(ns, key)
    except CacheUnavailable:
        return None                # 降级：cache miss

async def _cache_set_json(ns: str, key: str, value, *, ttl: int):
    try:
        cache = await get_cache()
        await cache.set_json(ns, key, value, ttl=ttl)
    except CacheUnavailable:
        pass                       # 降级：静默


async def heavy_compute(field: dict) -> dict:
    """缓存包裹：先查 Redis，miss 再算再回写。"""
    sig = hashlib.sha256(
        json.dumps(field, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    cache_key = f"profile:{sig}"

    cached = await _cache_get_json("my_domain", cache_key)
    if cached is not None:
        return cached

    result = _do_compute(field)              # 同步纯函数
    await _cache_set_json("my_domain", cache_key, result, ttl=_TTL_PROFILE)
    return result
```

**关键点**：

1. 缓存 helper 用 try/except `CacheUnavailable`，**永远不让缓存故障级联到上游**。
2. cache key 用 hash 而不是裸 dict 拼字符串 — 长度可控、稳定（见 §6.17）。
3. 三档 TTL（短/中/长）按业务"变更频率"分，不要所有缓存一个 TTL。
4. service 函数本身如果是 sync，加缓存前先评估要不要改成 async — 见 §6.16。
5. cache miss 路径上不要写「读 cache → 算 → 写 cache」三个 await 全程 try/except，
只在外层包一次；否则每个 await 失败路径都要重复一遍降级逻辑。
6. 把 sync 服务函数改 async 会触发测试链上所有调用方改动。改之前先
`Grep "\b<func_name>\("` 列出所有调用点；改完跑完整测试套件确认无遗漏。

## 11. 文档维护

- 这份文件本身有错、或遇到新坑没写进来 → **直接改**；不要在 PR 评论里口头说。
- 改了约定但没更新本文件 → 评审时会被打回。
- 命名 / 路径 / 工具变更，先改本文件再改代码。

## 12. 字段值相似度（HIA-72 B2）

### 12.1 什么时候用这些 helper

`src/services/candidates.py` 里的相似度 helpers 用于：

- **字段语义推断增强**：email/name/phone 等字段的值集合重合度高 → 跨表同语义字段检测
- **Primary Key 候选识别**：高 unique_ratio + 低 null_ratio + 特定字段名模式
- **置信度校准**：值集合高重合的字段互相加分（boost confidence）

### 12.2 四个核心 helper

| helper | 用途 | 返回值 |
|---|---|---|
| `_normalize_value(v)` | 归一化：None/空/null → None；str→lowercase+strip；int→str | `Optional[str]` |
| `_jaccard_similarity(set_a, set_b)` | 集合重合度 | `float` [0,1] |
| `_levenshtein_ratio(s1, s2)` | 字符串编辑距离相似度 | `float` [0,1] |
| `_field_value_overlap_ratio(field_a, field_b)` | 两字段 sample_values 的 Jaccard | `float` [0,1] |
| `_is_likely_primary_key(field)` | PK 候选判定 | `bool` |
| `_group_fields_by_value_overlap(fields)` | union-find 分组（同语义字段归组） | `list[list[str]]` |

### 12.3 Jaccard 阈值约定

`_FIELD_VALUE_OVERLAP_THRESHOLD = 0.7`

- ≥ 0.7 才视为"同语义"（经验值：两字段 70% 以上值重叠才可靠）
- 低于阈值：各自独立字段，不分组
- 跨表跨文件时先按字段名 + 数据类型过滤，再算 Jaccard（减少噪音）

### 12.4 PK 候选判定规则

满足任一即判为 PK：

1. **字段名模式**：`id`、`*_id`、`uuid`、`*_code`、`*_key`、`no`、`*_no`（正则匹配）
2. **高唯一 + 基本不空**：unique_ratio > 0.95 AND null_ratio < 0.1 AND 数据类型 string/int
3. **极高唯一**：unique_ratio > 0.9 AND null_ratio < 0.05

### 12.5 置信度加成规则

跨字段 Jaccard ≥ 0.7 时，confidence 额外 + `(overlap - 0.7) * 0.33`，封顶 +0.1。

示例：`label` 类型 baseline = 0.6，与其他字段 Jaccard = 0.85 → bonus = 0.05 → 最终 0.65。

### 12.6 避坑

- `_group_fields_by_value_overlap` 是 `O(n²)`，但 n 是字段数（一般 ≤ 50），可接受。
  如果未来扩展到大数据集，改为倒排索引。
- 字段值为 None/""/"null"/"nan" 的样本在 Jaccard 前会被过滤掉。
- `_is_likely_primary_key` 的高唯一判定要求 null_ratio 低 — 有大量 NULL 的"唯一"字段（如 optional_ref）不算 PK。

## 13. CR 工作流（HIA-69 / B5）

### 13.1 状态机

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──merge──▶ MERGED  (终态)
  │                  │
  │                  ├─changes_requested─▶ CHANGES_REQUESTED ──┐
  │                  │                                          │
  │                  └─close─▶ CLOSED (终态) ◀─close────────────┤
  │                                                           │
  └─close─▶ CLOSED (终态) ◀────────────────────────────────────┘
```

- `DRAFT` — 作者保存但未提交。
- `SUBMITTED` — open / 待审批。
- `CHANGES_REQUESTED` — reviewer 要求修改；作者改完可以 re-submit（reviewer 状态重置）。
- `APPROVED` — 所有 required_approvers 投票通过；等用户点 merge。
- `MERGED` / `CLOSED` — 终态，不可再变更。

### 13.2 多 reviewer 自动合并

API 两套路径互不覆盖：

- `POST /change-requests/{id}/approve` — 全局 approve。仅当
  `required_approvers == 0` 或没配 M2M reviewer 时可用；多 reviewer 时
  返回 400，强制走 per-reviewer 端点。
- `POST /change-requests/{id}/reviewers/{rid}/approve` — 单 reviewer
  投票。每次调用都触发 `_maybe_auto_merge(cr, session)`：APPROVED
  计数 ≥ required_approvers 即升 CR 到 `APPROVED`，并把 `approved_by`
  记成「最后那个把 CR 推过线的 reviewer」。

**避坑**：re-submit 一定要把所有 reviewer 状态重置成 `PENDING`，否则上
一轮的 `APPROVED` 票会被 `_approved_reviewer_count` 立刻算上，跳过
review。详见 §6.25。

### 13.3 评论线程（threaded comments）

`ChangeRequestComment` 是 self-referential adjacency-list 模型：

- 顶级评论：`parent_id = None`。
- reply：`parent_id` 指向上级评论；服务端校验 `parent_id` 必须指向同 CR 下
  的评论，否则 400（防止跨 CR 引用）。
- 软删除：`DELETE` 不真删，把 `deleted_at` 设为当前时间；list 时把
  `body` 替换成 `"[deleted]"` 保留 thread 结构。

**避坑**：self-referential `relationship` 不要加 `back_populates` —
SQLAlchemy 判定不了父/子方向，启动报 mapper error。本仓用单向
`replies = relationship("ChangeRequestComment", cascade="all, delete-orphan")`，
查询时显式 `WHERE parent_id == ?`。详见 §6.26。

### 13.4 API ↔ Model 一致性

**症状**：模型字段没改但 API 端点已经写好引用新字段 / 新 enum 值 —
启动时 import 不报错（端点要等真有人调才触发），调用端点报
`AttributeError: type object 'ChangeRequestStatus' has no attribute 'SUBMITTED'`。

**规避**：API 写完，**立刻**跑一次完整测试套件（不只是新增的测试）；
或者写一个端点级 smoke test（`from src.api.main import app; print(len(app.routes))`）
保证 import-time 不爆。具体见 §6.23 + HIA-69 PR description。

### 13.5 迁移：扩展现有 enum

PG 上扩展 enum 用 `ALTER TYPE ... ADD VALUE` 但必须在事务外（Alembic 默认
包事务 → 用 `execution_options(isolation_level="AUTOCOMMIT")` 绕开）。
SQLite 上 enum 是字符串 + CHECK 约束，扩展 enum 一般不需要碰 schema —
应用层校验即可。详见 §6.24。

## 14. Connector Snapshot → Evidence（HIA-67 / B2）

### 14.1 两条端点的语义差异

| 端点 | 用途 | 副作用 |
|---|---|---|
| `POST /connectors/{id}/snapshot` | 临时拉数据看（探索 / 调试） | 仅审计 READ，**不写库** |
| `POST /connectors/{id}/snapshot-to-evidence` | 把快照**固化**成 Source + Evidence | 写 Source + SourceSnapshot + N 条 Evidence（每字段一条） |

第二个端点是 HIA-67 验收点，**等价于文件上传的入库流程**：
走的是同一个 `Source` / `SourceSnapshot` / `Evidence` 表。

### 14.2 Source.connection_info 的"白化"原则

PG connector 的 config 里包含明文密码（解密后传入 connector）。
`snapshot-to-evidence` **写入数据库时不能复制原始 config**，否则：

1. 密码泄漏到 `sources.connection_info` JSON 字段 → `/projects/{id}/sources`
   接口可能把它暴露给前端。
2. 后续重读 source 时会用错凭据。

**做法**：写库时只存"白化连接信息"，如：

```python
safe_conn_info = {
    "type": "postgresql",
    "host": "...",
    "port": 5432,
    "database": "...",
    "schema": [...],          # schema_filter
    "table_filter": [...],     # table_filter
    # 显式 password / username 字段不写
}
# 然后 schema_info 记录 connector_id 让可追溯回原 connector
schema_info = {"table": body.table, "connector_id": str(conn.id), "connector_name": ...}
```

### 14.3 Evidence 字段推断

每个字段一条 `Evidence` 记录，`extraction_params` 存推断信息：

```python
extraction_params={
    "connector_id": "...",
    "pg_data_type": "integer",       # 原始 PG 类型
    "inferred_type": "int",          # 映射到本体类型
    "null_ratio": 0.05,
    "unique_ratio": 0.95,
    "sample_values": ["x", "y", "z"]
}
```

`_PG_TYPE_TO_MODEL` 映射表定义在 `connectors.py` 顶部，覆盖：

| PG | 本体 |
|---|---|
| integer / bigint / smallint | `int` |
| numeric / real / double precision | `float` |
| boolean | `bool` |
| character varying / text / character | `string` |
| date / timestamp[] / time[] | `date` |
| uuid / json / jsonb | `string` |

**未匹配的类型默认 `string`**（保守），前端可在 UI 里二次确认。

### 14.4 字段统计必须在客户端算

snapshot 接口返回的 `rows` 是 `dict[str, Any]`，不能假定服务端的 connector
能给你统计。`snapshot-to-evidence` 自己写循环算 `null_ratio` /
`unique_ratio`，原因：

- 服务端 connector 重复算会让 connector 接口膨胀
- 字段统计只对"快照这一批"有意义，没必要让 connector 实现者关心
- 一致性：所有 connector（csv / json / postgresql）走同一份统计代码

### 14.5 避坑

- **权限提升**：snapshot-to-evidence 需要 EDITOR（不是 VIEWER）。它写本体候选，
  不仅是读。**不要图省事用 VIEWER** —— 让 audit log 一查就发现越权。
- **路径隔离**：URL 是 `/connectors/{id}/snapshot-to-evidence?project_id=...`，
  必须用 `require_role_query(Role.EDITOR)`，**不要**用 `require_role`（path 版）。
- **审计**：写 `CREATE` 类型 audit event，`after` 字段含 source_id /
  snapshot_id / evidence_count，方便回溯。
- **PG connector 自带白名单**：`_passes_filter` 校验 schema/table 在白名单
  才让 snapshot。若用户调一张不在白名单的表，**前端会拿 400** 而非 500；
  错误信息直接显示 `ConnectorError` 消息即可。

### 14.6 测试覆盖清单

新加 connector → evidence 端点，**至少**覆盖：

1. **happy path**：snapshot + auto_evidence=true → evidence_count == column_count
2. **auto_evidence=false**：只创建 Source，不创建 Evidence
3. **跨项目 → 404**：用其他 project_id 访问 → 404
4. **类型映射**：PG 类型 → 本体类型映射正确（集成到所有 snapshot-to-evidence 测试）
5. **统计正确性**：null_ratio / unique_ratio / sample_values 在多行场景下准确

---

## 15. JWT + API Key 认证（HIA-64 B1）

M1 正式引入多用户认证。在 `X-User-Email` dev header 之**上**叠加两条
生产路径：**JWT**（人交互）+ **API Key**（机器对机器）。两者共存，旧的
dev header 仍 fallback 工作 — 不会破坏现有 dev / 测试。

### 15.1 认证优先级

`get_current_user` 严格按这个顺序判定，命中后立即返回：

1. `Authorization: Bearer <jwt>` — JWT access token，验证签名 + `type=access` + sub 是有效 active user。
2. `X-API-Key: ont_xxxxxxxxx...` — API Key，SHA-256 哈希后查 `api_keys.key_hash`；命中且未撤销未过期则取 owner user。
3. `X-User-Email` — dev header（向后兼容 HIA-51）。
4. `X-User-Id` — dev header。
5. bootstrap admin（`admin@ontolohub.local`）— **dev convenience**，生产环境应通过 JWT 登录。

实现见 `apps/api/src/api/auth.py::get_current_user`，**所有路径都复用**同一个 `_make_principal(session, user)` helper（注意是 `async def`，调用必须 `await`）。

### 15.2 JWT

- **库**：`python-jose`（HS256 对称签名）；不要用 `pyjwt` — 项目已统一 jose。
- **密码哈希**：`passlib[bcrypt]`；**`bcrypt<5.0`**（passlib 1.7.4 不兼容 bcrypt 5.x 的 `__about__` 属性，启动会 warning 但能跑）。
- **secret 来源**：JWT 优先用 `JWT_SECRET`（env），未设则退到 `SECRET_KEY + "-jwt"` 派生。**生产环境必须显式设 `JWT_SECRET`**，否则重启会丢签名。
- **双 token**：access TTL 默认 1h；refresh 默认 7d。refresh 仅用于换 access（`POST /api/auth/refresh`），不能直接调业务 API（`type != "access"` 会 401）。
- **payload**：`sub` 是 user UUID 字符串；`type` ∈ `{"access","refresh"}`；`email` 写进 access 便于审计 / 排查。

```python
# src/core/auth.py
def create_access_token(*, subject: str, extra: dict | None = None) -> str: ...
def create_refresh_token(*, subject: str) -> str: ...
def decode_token(token: str) -> dict: ...    # 抛 jose.JWTError
```

### 15.3 API Key

- **格式**：`ont_` + 32 字节 `secrets.token_urlsafe(32)`（约 40 字符总长）。
- **存储**：DB 存 SHA-256 哈希（`api_keys.key_hash`）；明文只在 `POST /api/api-keys` 创建响应里返回**一次**，list 接口只显示 `key_prefix`。
- **比对**：用 `hmac.compare_digest` 做 constant-time，避免 timing attack（`src.core.auth.constant_time_eq`）。
- **生命周期**：可设 `expires_in_days`（1–3650）；撤销走软删（设 `revoked_at`）；`is_active` 属性自动判断"未撤销且未过期"。
- **作用域**：`scopes: list[str]` 自由文本（M1 不做强校验），常用值：`read` / `write` / `connector` / `admin`。
- **project 绑定**：`api_keys.project_id` 为 NULL 时是全局 key；非 NULL 时用于 connector 跨项目场景（M1 暂未启用强制 scope，保留字段）。

```python
plain, key_prefix, key_hash = generate_api_key()
# 仅 plain 是明文 — 立即返回给用户；DB 只写 key_hash + key_prefix
api_key = ApiKey(user_id=..., name=..., key_hash=key_hash, key_prefix=key_prefix, scopes=[])
```

### 15.4 端点

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/auth/login` | POST | 邮箱 + 密码 → access + refresh |
| `/api/auth/refresh` | POST | refresh token → 新 access + 新 refresh |
| `/api/auth/me` | GET | 当前用户信息（任意已认证路径都可） |
| `/api/auth/set-password` | POST | 改自己密码（需 current_password）/ admin 代改别人 |
| `/api/auth/bootstrap` | POST | 创建 bootstrap admin（HIA-51 兼容，幂等） |
| `/api/api-keys` | GET / POST | 列 / 创建 API Key |
| `/api/api-keys/{id}` | DELETE | 撤销 API Key |

挂载在 `apps/api/src/api/auth_jwt.py`，`main.py` 已 include。改路由直接改这个文件。

### 15.5 路由组织 / 命名

- **登录失败语义**：永远 `401 invalid credentials`，**不区分**"邮箱不存在"和"密码错"，防 enumeration（`authenticate_user` 已经统一返回 None）。
- **改密码**：self 路径**强制**要 `current_password`；admin 代改要 `target_user_id`。两者都没有 → 400。
- **撤销幂等**：重复 DELETE 已撤销的 key → 204，不报错（前端可放心重试）。
- **API key 鉴权 401 提示**：`detail="invalid or expired API key"`，不区分"key 不存在"和"过期 / 撤销"。

### 15.6 避坑

#### 15.6.1 `_make_principal` 漏 `await`

**症状**：路由调 `get_current_user` 后访问 `principal.user` 报
`'coroutine' object has no attribute 'user'`。

**原因**：`get_current_user` 里 `return _make_principal(session, user)` —
`_make_principal` 是 `async def`，必须 `await`。

**规避**：所有 `return _make_principal(...)` 都写成 `return await _make_principal(...)`。
`auth.py` 已统一；新加分支时**逐个搜** `return _make_principal`，漏 await 不会被
类型检查抓到，运行时拿到一个 coroutine。

#### 15.6.2 `record_audit(project_id=...)` 必填 → 全局事件 401

**症状**：`POST /api/auth/login` 写审计时抛
`TypeError: record_audit() missing 1 required keyword-only argument: 'project_id'`。

**原因**：`record_audit` 原设计为 project-scoped 审计；login / api-key 撤销
这类**全局事件**没有 project_id。签名里 `project_id: Optional[uuid.UUID]`
是 keyword-only 但没有默认值，调用方必须显式传。

**规避**：`record_audit` 已把 `project_id` 改默认 `None`；调用方**不传** `project_id`
即可（None 表示全局事件，落到 `audit_events` 表 `project_id IS NULL` 的链）。

#### 15.6.3 bcrypt 5.x + passlib 1.7.4 不兼容

**症状**：`hash_password("...")` 抛
`AttributeError: module 'bcrypt' has no attribute '__about__'`（WARN 级别，
但实际 hash/verify 仍能工作）；或者直接抛
`ValueError: password cannot be longer than 72 bytes`。

**原因**：passlib 1.7.4 用 `_bcrypt.__about__.__version__` 探测版本，
bcrypt 5.x 移除了 `__about__`；fallback 路径里有别的隐式 bug，会把密码
按错误的字节数处理。

**规避**：`requirements.txt` 锁定 `bcrypt<5.0`（或 `bcrypt==4.3.0`）。
**不要**升 passlib — 上游已停止维护；也不要换 `bcrypt` 直调 — passlib
的 deprecated 提示还要保留。

#### 15.6.4 JWT secret 在生产环境必须显式

**症状**：dev 环境 JWT 一切正常，部署到生产重启后所有 token 401。

**原因**：默认 `effective_jwt_secret()` 退到 `f"{secret_key}-jwt"` 派生；
dev 默认 `secret_key="change-me-in-production"`；生产重启 secret_key 不
稳定 → 派生 secret 变 → 旧 token 全部失效。

**规避**：生产环境 `JWT_SECRET`（独立于 `SECRET_KEY`）必须显式设一个
稳定的、至少 32 字节的随机串；放进 secrets manager / `.env` 注入。

#### 15.6.5 测试里 bootstrap admin 没密码 → 走 JWT 路径 401

**症状**：测试 fixture 调 `ensure_bootstrap_admin()` 后 admin 立刻调
`/api/auth/login` 报 401。

**原因**：`authenticate_user` 校验 `user.password_hash` 不为空；
bootstrap admin 默认 `password_hash = NULL`（"尚未设密码"）。

**规避**：测试要测 JWT 流程前**手动给 admin 设密码**：

```python
from src.core.auth import hash_password
from sqlalchemy import select
from src.db.connection import async_session_factory
from src.db.identity import User

async with async_session_factory() as s:
    user = (await s.execute(select(User).where(User.email == "admin@ontolohub.local"))).scalar_one()
    user.password_hash = hash_password("test-password")
    s.add(user)
    await s.commit()
```

或者走 `POST /api/auth/set-password`（首次需 `current_password=""`，本仓库
endpoint 在该情况下可能 400 — 上面 SQL 路径最稳）。

#### 15.6.6 `app.include_router(auth_jwt.auth_router)` 不生效？

**症状**：`app.routes` 找不到 `/api/auth/login`，但 `app.openapi()['paths']`
能看到。

**原因**：FastAPI 把 `include_router` 后的子 router 包装成
`_IncludedRouter`（Route 子类），不出现在 `app.routes` 里；要查全部
路由用 OpenAPI schema 或 `app.router.routes`（也含 `_IncludedRouter`）。

**规避**：调试路由用 `app.openapi()['paths']`；写测试不需要关心，挂上就生效。

#### 15.6.7 枚举防御：`invalid credentials` 必须统一定义

**症状**：login 端点把"邮箱不存在"和"密码错"分两个 detail 暴露。

**规避**：所有失败都 `detail="invalid credentials"`。`authenticate_user`
已经统一返回 None；endpoint 不要因为 `user is None` 拆出 `user_not_found` /
`wrong_password` 两条 detail。`/api/auth/set-password` 改自己密码时 `detail="current password is wrong"` 也不应暴露原密码是什么。

#### 15.6.8 `python-jose` 选 `cryptography` extra

**症状**：`pip install python-jose` 后 import 报
`ImportError: cannot import name 'RSAAlgorithm' from 'jose.backends'`。

**原因**：默认 backend 是纯 Python `pyca`，某些算法（RS256 / ES256）需
要 `cryptography` extra。

**规避**：`pip install "python-jose[cryptography]"`。本仓只用 HS256
（对称），纯 Python backend 够用，但装上 `[cryptography]` 防止扩展到
非对称算法时再炸。

### 15.6.9 Alembic 改列 NOT NULL 必须先 backfill 再 drop default

**症状**：给已有 `users` 表加 `password_hash` / `last_login_at` 时直接写
`nullable=False`，结果：本地 dev SQLite 没数据看不出来；CI 跑
`alembic upgrade head` 之后如果 DB 已有数据（docker volume 还在），报
`IntegrityError: NOT NULL constraint failed`。

**原因**：SQLite / PG 都强制 NOT NULL；alembic migration 不管现有数据是否合法。

**规避**：标准三步走 — 加列 nullable → backfill 现有行 → 改 NOT NULL：

```python
# revision: add_user_password_and_api_keys.py
def upgrade():
    # 1. 加列，nullable=True
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    # 2. backfill（这里无所谓，反正没密码用户也是 NULL）

    # 3. 改 NOT NULL — 一定要分两步，先 server_default="" 再 alter
    with op.batch_alter_table("users") as batch:
        batch.alter_column("password_hash", existing_type=sa.String(255), nullable=True)  # 保持 True 是 OK 的
```

**最佳实践**：用户密码字段**永远 nullable=True**（用户可能在
"已建账号但还没设密码" 状态）；用应用层 `if user.password_hash is None`
判定"不允许走 JWT 登录"。强行 NOT NULL 会让 bootstrap 流程很别扭。

### 15.6.10 Docker Compose healthcheck 顺序依赖

**症状**：`docker compose up` 启动 api 容器后报
`asyncpg.exceptions.InvalidPasswordError: ... password authentication failed`，
但手动重启 api 又能连上。

**原因**：api 容器启动比 postgres 容器"完成 initdb"还早；asyncpg
第一次握手失败后没重试逻辑。

**规避**：

```yaml
services:
  api:
    depends_on:
      postgres:
        condition: service_healthy   # 等待 healthcheck 通过
      redis:
        condition: service_healthy
    # 同时在 api 启动脚本里加重试
    # entrypoint: ./start_api.sh  内部循环 30 次 retry on connection
```

`apps/api/start_api.sh` 模板：

```bash
for i in {1..30}; do
  python -c "import asyncio; from src.db import connection as c; asyncio.run(c.ping_db())" \
    && break
  echo "Waiting for DB... ($i/30)"
  sleep 2
done
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

### 15.6.11 `create_api_key` 后立刻返回 plain → 必须先 `flush` + `refresh`

**症状**：创建 API Key 后返回的 `plain_key` / `created_at` 是空字符串 /
None，前端拿到不能立刻显示。

**原因**：`session.add(api_key)` 不 `flush()`，DB 默认不生成 id /
`created_at`；`session.refresh(api_key)` 又是异步 lazy load。

**规避**：

```python
session.add(api_key)
await session.flush()         # DB 生成 id + created_at
await session.refresh(api_key)  # 把字段拉回 Python 端

return ApiKeyCreatedResponse(
    id=str(api_key.id),
    plain_key=plain,
    created_at=api_key.created_at.isoformat(),  # 现在才有值
)
```

任何「写 ORM 对象 → 立刻用对象字段做副作用（返回值 / 审计 / 计算）」的
模式都得照这个顺序，详见 §6.21。

### 15.7 测试模式

`tests/test_auth_jwt.py` 是参考模板，**至少**覆盖：

- 登录成功（邮箱+密码 → JWT pair）
- 登录失败（错密码 / 未知邮箱 — 都返同样的 401）
- 没密码的用户不能走 JWT 登录（401）
- `Authorization: Bearer` 鉴权读取 `/me`
- `X-API-Key` 鉴权读取 `/me`
- `Bearer` 优先于 `X-API-Key`（同时给两个，Bearer 生效）
- `refresh_token` → 新 access
- `access_token` 当 refresh 用 → 401
- 改自己密码需 current_password；错密码 → 401
- admin 代改别人密码（target_user_id）
- API Key：创建只返一次明文 / list 不含明文
- API Key：revoke 后不能再用
- API Key：重复撤销幂等
- API Key：expires_in_days 写进 expires_at
- API Key：未知 id 撤销 → 404

fixture 模板见 `tests/test_auth_jwt.py::client` — 独立 SQLite + alembic
upgrade head + bootstrap admin + `ASGITransport`，**不要**复用
`test_auth_isolation_audit.py` 的 `isolated_app`（它假设 header-based
认证，JWT 测试需要给 admin 设密码）。

---

## 16. Release + Deployment + Preflight（HIA-61 / A11）

### 16.1 模块边界

- `apps/api/src/api/release.py` — 所有端点 + Pydantic schema
- `apps/api/src/db/release.py` — `Release` / `Deployment` / `UseCaseBundle` / `PreflightReport` ORM 模型
- **路由前缀统一 `/releases`**，子路由：
  - `/releases/projects/{project_id}/releases` — Release 列表/创建
  - `/releases/releases/{release_id}` — Release 详情/更新
  - `/releases/releases/{release_id}/download` — manifest 下载
  - `/releases/releases/{release_id}/preflight` — 预检
  - `/releases/releases/{release_id}/publish` — 发布
  - `/releases/projects/{project_id}/deployments` — Deployment 列表/创建
  - `/releases/deployments/{deployment_id}` — Deployment 详情/更新/回滚
  - `/releases/projects/{project_id}/use-case-bundles` — UseCaseBundle CRUD
  - `/releases/projects/{project_id}/preflight-reports` — PreflightReport 列表

### 16.2 Release 状态机

```
DRAFT ──publish──► BUILT ──(内部)──► RELEASED
                       │
                       ├──preflight──► PREFLIGHTING ──► PREFLIGHT_FAILED
                       │                       │
                       │                       └─► PASSED
                       └──deploy──► DEPLOYING ──► DEPLOYED / FAILED
```

- `DRAFT` 是新建时的初始状态；`BUILT` 由 `publish` 端点写入；
  `RELEASED` 在 BUILT 后所有校验通过时写入。
- 已 `RELEASED`/`PUBLISHED` 的 release **不可修改 description / tags**（PATCH 返回 400）。
- Deployment 只接受 `RELEASED` 状态的 release；`DRAFT` / `BUILT` → 400。

### 16.3 tags 字段的特殊处理

- `Release.tags` **不存数据库列**，统一写到 `artifacts["tags"]`。
- PATCH 时：
  ```python
  current = release.artifacts or {}
  release.artifacts = {**current, "tags": value}
  ```
  必须**整体赋值**（见 §6.28 — SQLAlchemy JSON in-place 不被检测）。
- 不要把 `tags` 当成 ORM 字段加到模型上 — 它会破坏 manifest 反序列化逻辑。

### 16.4 Deployment 模型没有 `release` relationship

- `Deployment` 只有 `release_id` 外键列，**没有**显式 `relationship("Release")`。
- 任何 `selectinload(Deployment.release)` 都会 AttributeError（见 §6.29）。
- 列出 deployment 时直接 `.join(Release).where(Release.project_id == ...)`
  就够了；不需要预加载。

### 16.5 UseCaseBundle 必须带 release_id

- `UseCaseBundle.release_id` 在 DB 里是 NOT NULL + ForeignKey。
- Create schema `UseCaseBundleCreate` 必填 `release_id: uuid.UUID`。
- 创建端点必须先 `_verify_release_exists` 再校验 `release.project_id == project_id`
  （跨项目 bundle → 400）。

### 16.6 Preflight 当前是 placeholder

- `release/{rid}/preflight` 返回 4 项 hardcoded check：
  ontology_version / mapping_version / database_connectivity / artifact_integrity，
  后两个永远 pass。
- 这是占位实现，等 HIA-58 (SHACL 校验执行) 完成后接入真实检查。
- 客户端调用按这个 shape 解析；改字段前通知 web 前端。

### 16.7 集成测试

- `tests/test_release_deployment.py` — 17 个测试覆盖 CRUD / preflight / deploy /
  cross-project isolation / 入参校验。每个测试独立 SQLite + alembic up head。
- 写新端点前先看这个文件的 fixture 模板；保持 `isolated_app` → `client` 二级
  fixture 结构。

### 16.8 避坑速查

| 症状 | 原因 | 修法 |
|---|---|---|
| PATCH tags 后还是空 | SQLAlchemy 检测不到 in-place JSON 改动（§6.28） | 整体赋值 `{**current, "tags": value}` |
| list deployments 500 AttributeError | `Deployment` 没 `release` 关系（§6.29） | 删 `selectinload(Deployment.release)`，只用 `.join()` |
| 创建 use-case-bundle NOT NULL 失败 | schema 缺 `release_id`（§6.30） | 加 `release_id: uuid.UUID` 必填 |
| 发布后 PATCH 400 | release.status 已是 RELEASED | 用 PUT 走 update，或先 unpublish |

## 17. HIA-56 / A7 Ontology Version CRUD 端点（HIA-56 收尾补全）

### 17.1 新增端点（HIA-56 在原 fork/diff 基础上补充）

`apps/api/src/api/ontologies.py` 末尾新增三个端点（commit 904d65a）：

| 方法 | 路径 | 用途 | 状态校验 |
|---|---|---|---|
| POST | `/ontologies/{id}/versions` | 基于 head 创建草稿版本 | 复制最新 published 快照 |
| PUT | `/ontologies/{id}/versions/{vid}/content` | 编辑器保存快照 | 仅 DRAFT 可改 |
| POST | `/ontologies/{id}/versions/{vid}/publish` | 发布特定版本为 head | 仅 DRAFT 可发布 |

### 17.2 测试覆盖

`apps/api/tests/test_ontology_versions.py` 12 个集成测试，覆盖：
- list_versions_empty（新本体无版本）
- create_draft_version + create_draft_version_inherits_snapshots
- update_version_content + update_version_content_rejects_published
- publish_version + publish_version_rejects_published
- 404/400 错误路径 + diff + fork 端点

### 17.3 端点设计原则

- **DRAFT 是可变的，PUBLISHED 是不可变的**：所有写操作先校验 `status == DRAFT`
- **快照字段是 source of truth**：DRAFT 的 class_snapshot 等字段就是编辑器工作区
- **发布即冻结**：publish 后改 ontology 表的 version + status 字段，但不动 snapshot
- **历史版本永远保留**：从不删 OntologyVersion，所有版本可对比

### 17.4 与现有 publish 端点的关系

- 旧 `POST /ontologies/{id}/publish` 仍存在：从当前 DB 状态生成快照后发布，要求 ontology 不在 PUBLISHED 状态
- 新 `POST /ontologies/{id}/versions/{vid}/publish` 走 DRAFT 路径：发布 OntologyVersion 行本身，不修改 ontology 实际内容
- **两者并存但语义不同**：测试中发布第二个版本用新路径（先 POST /versions 创建草稿，再 POST /versions/{vid}/publish 发布）

## 18. 测试 isolation 偶发失败的根因分析

### 18.1 现象

跑 `python -m pytest apps/api/tests/` 全量测试时，1-2 个测试偶发：
- `IntegrityError: NOT NULL constraint failed: xxx.release_id`
- 或 `KeyError: 'id'` 但单独跑该测试又总通过

### 18.2 根因

`isolated_app` fixture 调用 `await conn.reinit_engines()` 重建 engine，但：
1. Python `asyncio` 在 Windows 默认用 `SelectorEventLoop`，async fixture 之间的调度顺序非确定
2. 旧 engine 的 `async_session_factory` 可能还在引用，被新测试短暂复用
3. pytest 默认按收集顺序跑测试，文件/模块间共享 `import` 的 connection 模块状态

### 18.3 验证

- 单独跑失败测试：`pytest tests/test_xxx.py::test_yyy` → 100% 通过
- 调换测试文件运行顺序：失败可能换到别的测试
- 加 `--random-order` 插件：失败模式随机

### 18.4 应对

- **不要花时间追**：已确认是环境问题，不是代码 bug
- CI 上若稳定失败：在 `isolated_app` fixture 里加 `await asyncio.sleep(0.01)`
- 写新测试时：始终跑全量 + 单独跑两边都通过才算完成

### 18.5 已踩过的实例

- `test_create_and_list_use_case_bundle`：批量跑时 `release_id` 报 NULL，单跑通过
- `test_create_and_list_deployment`：同上

## 19. 项目级约定（写在根目录 CLAUDE.md）

`CLAUDE.md` 给 AI Agent 看，**简洁**（~80 行），列最关键陷阱。完整规范见本文档。

## 20. 关于 "AI Agent 工作流"

- **不要假设**：所有结论都要从代码/Linear/CI 验证
- **不要重复提交同样 commit**：rebase + amend 而不是新增 fixup
- **不要 hardcode 用户身份**：用 `X-User-Email` header 让 fixture 注入
- **不要 mock 数据层**：测试用真 SQLite + alembic up head
- **不要直接 dump ORM 对象到 response**：必须经 `_to_response()` helper 显式构造

## 21. HIA-57 / A10 Change Request Diff 端点（HIA-57 收尾补全）

### 21.1 新增端点

`apps/api/src/api/release.py` 新增 `GET /change-requests/{cr_id}/diff`：

- 优先返回 `ChangeRequest.diff` JSON 字段（已预计算结果，`computed_from="stored"`）
- 否则查 `baseline_version_id` / `target_version_id` 对应的 `OntologyVersion`，从快照算 diff（`computed_from="snapshots"`）
- 若两者都没设置，回 422

### 21.2 关键设计

- **`ChangeRequest.baseline_version_id` / `target_version_id` 没有显式 FK**：按 HIA-69 设计可指向 OntologyVersion / MappingVersion 等；本端点优先按 OntologyVersion 解析
- **快照对比覆盖 class / property / relation 三维度**：`urn`/`iri` 作为 key，相同 iri 不同内容视为 `modified`（带 `details.before` / `details.after`）
- **约束对比暂略**：可在 ontologies.py::diff_ontology_versions 已有逻辑上扩展，CR diff 视图暂时不需要

### 21.3 测试覆盖（21 个 CR 测试）

新增 6 个：

- `test_diff_helper_directly_unit` — `_diff_snapshots` 单元测试
- `test_cr_diff_404_for_nonexistent_cr` — CR 不存在
- `test_cr_diff_422_without_versions` — 缺 baseline/target
- `test_cr_diff_returns_stored_diff` — 命中 stored 路径
- `test_cr_diff_computes_from_snapshots` — 命中 snapshots 路径
- `test_cr_diff_404_when_version_record_missing` — 版本行被删

### 21.4 测试踩坑

跨 session 写数据必须 **`commit()`**，不能只 `flush()`。HTTP 请求走独立 session，flush 仅事务内可见。

```python
async with session_factory() as s:
    cr = await s.get(ChangeRequest, uuid.UUID(cr_id))
    cr.diff = {"entries": [...], "summary": {...}}
    await s.commit()  # ← 不要 flush
```

---

## 22. Webhook / Trigger 集成（HIA-75 / C3）

Webhook 分两路：**Out**（主动发到外部 URL）和 **In**（外部 POST 进来触发）。
两路都用 `trigger_config` + `action_type_id` 关联 ActionRun。

### 22.1 模块边界

- `apps/api/src/db/webhook.py` — `WebhookConfig` / `WebhookDelivery` / `TriggerConfig` ORM
- `apps/api/src/services/webhook_dispatcher.py` — HMAC 签名 + 重试 + 内嵌处理
- `apps/api/src/api/webhooks.py` — CRUD + 内嵌 webhook 接收端点
- `apps/api/alembic/versions/2026_09_18_0007_webhook_trigger.py` — schema

### 22.2 Out 端：HMAC-SHA256 + 指数退避重试

每次发 webhook 构造签名头：

```python
timestamp = str(int(datetime.now(timezone.utc).timestamp()))
signed_payload = f"{timestamp}.{body}"
signature = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
headers = {
    "Content-Type": "application/json",
    "X-OntoloHub-Signature": f"sha256={signature}",
    "X-OntoloHub-Timestamp": timestamp,
    "User-Agent": "OntoloHub-Webhook/1.0",
}
```

**接收端校验**：用 `replay-attack-safe` 顺序：
1. 检查 `X-OntoloHub-Timestamp` 在 ±5 分钟内（防重放）
2. 重算 `HMAC(secret, "{timestamp}.{body}")` 与 `X-OntoloHub-Signature` 比对
3. `hmac.compare_digest` 防 timing attack

### 22.3 重试策略 + 状态机

`WebhookDelivery.status` 状态机：

```
PENDING ──HTTP 2xx──► SUCCESS (终态)
   │
   ├──HTTP 非 2xx/timeout──► RETRYING ──成功──► SUCCESS
   │                              │
   │                              └─attempt < retry_count──► RETRYING
   │
   └──attempt == retry_count──► DROPPED (终态)
```

退避：`asyncio.sleep(retry_delay * (2 ** (attempt - 1)))` — 1min / 2min / 4min。

### 22.4 SQLite 存 UUID 是 32 字符 hex（无 dash）— 用 `.hex` 转换

**症状**：`webhook_dispatcher.py` 用 raw SQL 写 `trigger_configs` 时，
`UUID("...")` 直接传给 `:id` 绑定变量报 `ValueError: badly formed hexadecimal UUID string`。

**原因**：SQLAlchemy 在 `Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))` 上
**自动**用 `.hex` 存储；如果你绕过 ORM 用 `session.execute(_text("... WHERE id = :id"), {"id": some_uuid})`，
aiosqlite 不会自动转换，传 `uuid.UUID` 对象会报类型错，传 str-with-dash 也会因为长度不对报 `badly formed`。

**规避**：

```python
# ✅ 用 .hex 拿到 32 字符无 dash 的字符串
session.execute(
    _text("INSERT INTO trigger_configs (id, ...) VALUES (:id, ...)"),
    {"id": matched_trigger_id.hex, ...}
)

# 读回来时反过来
matched_trigger_id = uuid.UUID(row[0])  # row[0] 是 32 字符 hex
```

PG 上不存在这个问题（用 `uuid` 类型，自动接 `UUID` 对象）。SQLite + raw SQL
混用时才需要 `.hex` 转换。

### 22.5 内嵌 webhook 处理用 sync session 而非 async

**症状**：`POST /api/webhooks/in/{token}` 测试里第一次触发就报
`TriggerConfig token not found`，但其实 trigger 刚刚在同一个 HTTP 响应里创建成功。

**原因**：FastAPI 异步 handler 的事务还没 commit（要等 `Depends(get_session)` 的
get_session generator 退出），下游 `process_inbound_webhook` 已经起了一个
**新的 async session**，看不到未提交的 INSERT。

**规避**：内嵌 webhook 处理函数（`process_inbound_webhook`）**用 sync session**
（`sync_session_factory`）而不是 async —— sync session 走的是另一个连接池，
强制等当前 async 事务提交后才会发新查询。

```python
from src.db.connection import sync_session_factory
from sqlalchemy import text as _text

with sync_session_factory() as session:
    row = session.execute(
        _text("SELECT id, action_type_id, project_id, input_template "
              "FROM trigger_configs "
              "WHERE trigger_type = 'INBOUND_WEBHOOK' "
              "  AND json_extract(trigger_config, '$.token') = :token"),
        {"token": token},
    ).first()
```

**反例**（不要照抄）：

```python
# ❌ async session 看到的是 isolation level 内的快照，
# 触发器还没 commit 之前看不到
async with async_session_factory() as s:
    row = (await s.execute(select(TriggerConfig).where(...))).first()
```

### 22.6 Trigger input_template：`{{webhook.payload.xxx}}` 简单替换

`_apply_template(template, context)` 是递归 dict / list 替换，只识别
`{{var.path}}` 这种字符串模板；不识别分支 / 循环 / 表达式。

```python
# 示例
input_template = {
    "customer_email": "{{webhook.payload.email}}",
    "metadata": {
        "received_at": "{{webhook.received_at}}",
        "headers": {
            "user_agent": "{{webhook.headers.user-agent}}"
        }
    }
}
```

**支持的上下文变量**（按 trigger 类型）：

- **inbound_webhook**：`{{webhook.headers.xxx}}` / `{{webhook.payload.xxx}}` / `{{webhook.received_at}}`
- **schedule**：`{{schedule.fired_at}}` / `{{schedule.cron}}`
- **object_change**（计划）：`{{object.before}}` / `{{object.after}}` / `{{object.event_type}}`

**未匹配的处理**：保留原字符串 `{{unknown.var}}` 不替换（不报错）；要业务侧
校验时再用 `{{var}}` 检查结果是否含 `{`。

### 22.7 cron 解析只支持基础 5 字段语法

`_cron_matches` 实现简化的 cron 匹配，**只支持**：

- `*` — 通配
- `*/N` — 每 N 单位
- 逗号分隔的列表 `1,3,5`
- 精确值 `5`

**不支持**：范围 `1-5`、L / W / # 扩展、时区处理、秒级 cron（仅 5 字段：`分 时 日 月 周`）。
**周字段**：Sunday = 0（不是 7）。

```python
# 支持
"*/5 * * * *"     # 每 5 分钟
"0 9 * * 1-5"     # ❌ 不支持范围 — 当前实现会判错
"0 9 * * 1,3,5"   # ✅ 周一周三周五 9 点
```

需要高级 cron 时换 apscheduler / `croniter` 库。

### 22.8 Inbound webhook 接收端点无 auth — token 在 URL 里

**设计**：`POST /api/webhooks/in/{token}` 是**公开端点**，没 `Authorization` header。
鉴权完全靠 URL 中的 token：

```python
@trigger_router.post("/api/webhooks/in/{token}")
async def receive_webhook(token: str, request: Request):
    # 不走 require_role / get_current_user — 外部系统不会发这些 header
    success, error, trigger_id = await process_inbound_webhook(token, payload, headers)
```

**安全要求**：

- Token 用 `secrets.token_urlsafe(32)`（256 bit 熵），不要可枚举
- token 泄漏 = 攻击者可触发你的 Function；考虑 rate limit + IP allowlist
- 配置 trigger 时默认生成 token，用户也可指定（但**不要**复用旧 token）
- 收到 404 时**统一**返 `"Token not found or trigger not active"`，
不区分"token 不存在"和"trigger 暂停" — 防 enumeration

### 22.9 UUID(as_uuid=True) 处理器在 dispatcher 必须先转

**症状**：`WebhookConfig.project_id` 在 ORM 里是 `UUID(as_uuid=True)`，dispatcher
用 `select(WebhookConfig).where(WebhookConfig.project_id == project_id)` 查
的时候，传字符串 `project_id` 报 `ValueError`。

**原因**：`UUID(as_uuid=True)` 列的处理器对绑定的字符串调用 `.hex`；如果传
`uuid.UUID(...)` 对象本身，SQLAlchemy 会试图 `.hex` 一个 UUID 实例（UUID 没
`hex`，但有 `.hex` 属性 → 返回 32 字符 hex），所以传 UUID 对象也能跑；如果
传**带 dash 的字符串**，SQLAlchemy 会先解析再 .hex → 也 OK。

**真正出错的是 SQLAlchemy 2.0 的 strict 类型检查** — 传 `str` 而不是 `UUID` 会抛
`InvalidRequestError: expected UUID, got str`。

**规避**：在 dispatcher 入口统一转：

```python
if isinstance(project_id, str):
    project_id = uuid.UUID(project_id)
```

### 22.10 测试用例：清理未完成的 delivery task

**症状**：`test_webhook_trigger.py` 跑完测试后，控制台报 `RuntimeError: Event loop is closed`
或者 task `was destroyed but it is pending!`。

**原因**：`dispatch_webhook` 用 `asyncio.create_task(_deliver_with_retry(...))` fire-and-forget，
测试 fixture 退出后 ASGITransport 关闭 event loop，但 task 还在 sleep / retry 中。

**规避**（测试 fixture 里）：

```python
@pytest_asyncio.fixture
async def isolated_app(...):
    async with AsyncClient(...) as client:
        yield client
    # 等待所有 webhook delivery task 结束
    pending = [t for t in asyncio.all_tasks() if not t.done() and "_deliver_with_retry" in str(t)]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
```

或者在 production webhook URL 用 `[http://localhost:0/never-resolve]` 等极快失败的
URL，避免测试卡在 sleep / retry 循环。

---

## 23. Function 沙箱执行器（HIA-78 / C2）

HIA-78 实现 Python / JS Function 的 subprocess 沙箱执行。真正的安全边界是
**subprocess + timeout + (Unix) rlimit**；AST 限制**不做**（RestrictedPython
会引入过多兼容性问题，收益不抵复杂度）。

### 23.1 模块边界

- `apps/api/src/runtime/sandbox.py` — `execute_python()` / `execute_javascript()`
- `apps/api/src/runtime/result.py` — `SandboxResult` / `SandboxError`
- 子进程通过 stdin/stdout 协议与主进程通信（见 §23.5）

### 23.2 资源限制矩阵

| 限制项 | Unix 实现 | Windows 实现 |
|---|---|---|
| Wall-clock timeout | `subprocess.run(timeout=N)` | `subprocess.run(timeout=N)` |
| CPU time | `resource.RLIMIT_CPU` | ❌（靠 timeout 兜底） |
| Memory (地址空间) | `resource.RLIMIT_AS` | ❌（靠 timeout 兜底） |
| 文件描述符 | `resource.RLIMIT_NOFILE` | ❌ |
| 输出大小 | stdout/stderr 截断 1MB | stdout/stderr 截断 1MB |
| 阻止 fork | `prctl(PR_SET_NO_NEW_PRIVS=38, 1)` | ❌ |

**Windows 上的妥协**：靠 timeout + 输出截断做兜底；用户可以写死循环 / 占满内存
但 30s 后必被 kill。本机验证足够，生产 Linux 部署才完整生效。

### 23.3 编译期只做 `ast.parse`，不做 RestrictedPython

```python
def validate_python_source(code: str) -> None:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise SandboxError(kind="syntax", message=f"Syntax error: {exc.msg}")
```

**为什么不限制 import / getattr / `_` 开头属性**：

- RestrictedPython 会强制覆盖 `__builtins__`，破坏 `print`、`len`、`json.dumps`
  这些常用内置；用户 Function 90% 都不安全也不需要。
- 真隔离靠 subprocess — 子进程崩了主进程没事，timeout 强制 kill。
- 业务上要让用户能 `import json` / `import requests` 调外部 API。

**真正需要隔离的**（如果出现）：

- 文件系统访问 — 加 chroot / docker
- 网络访问 — 加 network namespace
- 资源配额持久生效 — Linux cgroup

### 23.4 输入输出协议

**主进程 → 子进程（stdin）**：

```json
{"input_data": {...}, "secrets": {...}, "timeout_s": 30}
```

**子进程 → 主进程（stdout）**：

```
[user print output, free-form]
<<<RESULT>>>
{"final": "result value", ...}
<<<END>>>
```

**为什么用 marker 而不是只读 stdout 最后一行**：

- 用户 `print("...")` 可能输出任意内容，最后一行不可靠
- marker 让结果边界清晰；主进程抓 `<<<RESULT>>>...<<<END>>>` 区间
- 用户代码最后必须赋值给 `result` 变量；runner 模板负责 `json.dumps(globals_dict["result"])`

### 23.5 用户代码嵌入 runner 模板用 `repr` 双层转义

```python
def _build_runner_script(user_code: str) -> str:
    embedded = repr(user_code)  # 'a = 1\\nresult = a + 1'
    return _PYTHON_RUNNER.replace("USER_CODE_PLACEHOLDER", embedded)
```

`repr()` 把字符串转成合法 Python 字面量 —— 处理换行 / 引号 / 反斜杠不踩坑。
**不要**用 f-string 拼接或 `"..." + code + "..."`，多行代码 + 单引号
会立刻破坏语法。

### 23.6 Node.js 24 在 Windows 上 stdin 触发 CSPRNG 断言

**症状**：`execute_javascript()` 在 Windows + Node 24 上跑（哪怕最简单的代码）：
```
internal/crypto/random.js: ... Error: ... Failed to generate bytes
```

**原因**：Node 24 启动时 stdin pipe 触发 CSPRNG 初始化；某些 Windows 环境
（特别是 git-bash / 容器内）默认 stdin 不可读。

**规避**：`stdin=subprocess.DEVNULL` + 把 input 写到**临时文件**：

```python
input_fd, input_path = tempfile.mkstemp(suffix=".json", prefix="sandbox_js_in_")
with os.fdopen(input_fd, "w") as f:
    f.write(json.dumps({"input_data": input_data or {}}))

sandbox_env["__HIA78_INPUT_PATH__"] = input_path

proc = subprocess.run(
    ["node", wrapper_path],
    capture_output=True,
    timeout=timeout,
    stdin=subprocess.DEVNULL,  # ← 关键
    env=sandbox_env,
)
```

JS wrapper 脚本从 `process.env.__HIA78_INPUT_PATH__` 读 input。

### 23.7 沙箱子进程环境变量白名单

```python
def _sandbox_env() -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),  # Windows 必须
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "PYTHONPATH": "",     # 禁掉外部包路径
        "HIA78_SANDBOX": "1", # 标识
    }
```

**关键点**：

- **不**传 `SECRET_KEY` / `DATABASE_URL` / `JWT_SECRET` 等凭证到子进程
- Windows 上 `SYSTEMROOT` 必传，否则很多 API（包括 subprocess）失败
- `PYTHONPATH=""` 强制子进程用系统默认 Python 路径，不被宿主污染

### 23.8 用户代码失败的 3 类异常

```python
# 1. 编译失败（语法错）
SandboxError(kind="syntax", message="Syntax error: ...")

# 2. 运行时异常（exec 阶段）
#    子进程 sys.exit(1) 写 traceback 到 stderr，主进程原样抛回
SandboxResult(exit_code=1, stderr="Traceback ...\nValueError: ...", ...)

# 3. 超时
SandboxError(kind="timeout", message=f"Execution exceeded {timeout}s timeout")
```

**ActionRun.status 映射**：

- 1 / 2 → `ActionRunStatus.FAILED`（业务失败，error 字段记 stderr）
- 3 → `ActionRunStatus.FAILED`，error 写 `timeout_ms={duration_ms}` 便于排查

### 23.9 ActionRun.input_data JSON 序列化要稳定

**症状**：函数 sandbox 拿到的 `input_data` 是 dict 但字段顺序变了，
函数内 `assert input_data == {"a": 1, "b": 2}` 失败。

**原因**：SQLAlchemy 把 `Mapped[dict] = mapped_column(JSON)` 的 JSON 列
反序列化时按入库时的 JSON 字符串还原；如果入库是 `json.dumps(data, sort_keys=False)`，
反序列化后字段顺序就是入库顺序。

**规避**：写入 ActionRun 时统一 `json.dumps(input_data, sort_keys=True)`，或
让用户函数按 key 取值而不是 assert dict literal。

### 23.10 sandbox 测试要跳 Windows-only 的子进程路径

**症状**：CI 跑 sandbox 测试全过，本地 Windows 跑 `subprocess.run([sys.executable, tmp_path], ...)`
挂起或超时。

**原因**：本机 Python 安装了某些包带 debugger（pydevd）会卡 subprocess。

**规避**：测试前 `pip uninstall pydevd pydevd-pycharm` 或者用
`subprocess.run([sys.executable, "-S", tmp_path], ...)`（`-S` 不加载 site）。

---

## 24. 通用避坑（跨任务总结）

### 24.1 UUID(as_uuid=True) 在 SQLite 上的存储行为

`Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))` 在 SQLite 里
**默认存 32 字符 hex（无 dash）**；UUID 类型本身变成 `CHAR(32)`。这一点
和 PostgreSQL `uuid` 类型存 `xxxxxxxx-xxxx-...` 字符串**不一致**。

**踩坑位置**：

1. **Alembic 迁移**：SQLite 表 schema 里看到的是 `CHAR(32)`，迁移到 PG
   时要 `op.alter_column` 改类型；或者一开始就不要手动写 schema 让 autogenerate 来。
2. **Raw SQL**：绕过 ORM 用 `session.execute(_text(...))` 时，绑定
   `uuid.UUID(...)` 对象 — aiosqlite 上**不会**自动 `.hex`，必须手动 `id.hex`。
3. **跨 dialect 测试**：PG 测试和 SQLite 测试都跑 — 任意一边 fail 都是
   dialect 差异问题。

### 24.2 HTTP 端点的 tag 顺序与 OpenAPI

FastAPI 按**装饰器注册顺序**生成 OpenAPI `paths` 字典，顺序与 `tags` 显示
无关 — tags 是按字母排序聚合。如果想让 `/api/webhooks/in/{token}` 出现在
"Triggers" 而非 "Webhooks" tag 下，**必须**用不同的 `APIRouter` 实例
（`trigger_router = APIRouter(tags=["Triggers"])`），不要想靠 `@router.post`
覆盖 tag。

### 24.3 测试用 fake URL 触发 webhook，端口要空闲

**症状**：`test_webhook_dispatch` 用 `http://127.0.0.1:9999/hook` 测试，
本机端口被占用，httpx 报 `ConnectError`，但测试还跑通了（因为 dispatcher
catch 后只记 `failed_deliveries`，不影响主流程）。

**规避**：测试里启动一个真正的 aiohttp / uvicorn mock server：

```python
import aiohttp
from aiohttp import web

received_payloads = []

async def hook(request):
    payload = await request.json()
    received_payloads.append(payload)
    return web.Response(text="ok")

app = web.Application()
app.router.add_post("/hook", hook)

@pytest_asyncio.fixture
async def webhook_server():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)  # 端口 0 = 让系统分配
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/hook"
    await runner.cleanup()
```

不要 hardcode 固定端口；本机 CI / 多测试并行跑都会冲突。

### 24.4 异步 fire-and-forget task 要保留 ref 到测试结束

```python
# ❌ task 失去引用，GC 可能在中间干掉
asyncio.create_task(_deliver_with_retry(...))

# ✅ 保留 ref（模块级 list 或 instance attribute）
_DISPATCH_TASKS: set[asyncio.Task] = set()

def fire_delivery(...):
    task = asyncio.create_task(_deliver_with_retry(...))
    _DISPATCH_TASKS.add(task)
    task.add_done_callback(_DISPATCH_TASKS.discard)
```

production OK（loop 一直跑），测试会爆 `Task was destroyed but it is pending`。
HIA-75 webhook dispatcher 已用 `await asyncio.gather(*tasks, return_exceptions=True)`
但 fire-and-forget 路径上仍要保留 ref 防止 GC。

### 24.5 SQLite 上 JSON 列查询的代价

`WHERE json_extract(trigger_config, '$.token') = :token` 在 SQLite 上
**全表扫描**（json path 上无索引）；生产 PG 上可加 GIN 索引。

如果 trigger_configs 表会很大（>10K 行），dispatcher 入口要加 LRU 缓存：

```python
from functools import lru_cache
import asyncio

@lru_cache(maxsize=1024)
def _cached_token_lookup(token: str) -> Optional[str]:
    # 注意：sync 函数不能直接 await；改成 async + 自建 cache
    ...
```

实际生产方案：dispatcher 启动时把 `token → trigger_id` 全量加载到内存 dict，
token 增删时同步更新内存 map（监听 webhooks API 的 INSERT/DELETE）。

### 24.6 Branch 命名：Linear 卡和 git 分支名必须严格匹配

**症状**：上一步 HIA-78 提交到 `liaxiao23/hia-78-c2-...` 分支后，
下一个任务 HIA-75 继续在同一分支写代码，commit 提交时分支名仍是 HIA-78。
结果 HIA-75 的 commit 落在 HIA-78 分支上，git blame / log 全部串了。

**规避**：每接新卡先 `git checkout -b liaxiao23/hia-XX-<name> <base>`，
**base** 用上一个已 Done 的分支 HEAD（不一定是 main — 可能是栈式分支）。
不要 `git checkout -b ... main` 然后 rebase —— 如果中间有别人的提交会冲突。

**校验**：`git log --oneline | grep "HIA-75"` 看 commit 是否在正确的分支上。

## 25. Workflow 编排（HIA-76 / C4）

把多个 `ActionType` / `WebhookConfig` / object API / delay 串成顺序执行的有向无环图（当前仅顺序执行 + 跳步；并行/循环/条件分支在 backlog）。

### 25.1 模块边界

- `apps/api/src/db/workflow.py` — `Workflow` / `WorkflowExecution` / `WorkflowStepResult` ORM
- `apps/api/src/runtime/workflow_step_handlers.py` — 4 种 step 类型的 handler
- `apps/api/src/runtime/workflow_executor.py` — 顺序执行引擎
- `apps/api/src/api/workflow.py` — CRUD + 执行端点
- `apps/api/alembic/versions/2026_09_18_0008_workflow.py` — schema + trigger_configs.workflow_id

### 25.2 Step 类型与字段

| type | 必填 ref | 必填 config | 说明 |
| --- | --- | --- | --- |
| `function_call` | `ActionType.id` (kind=function) | 可选 `timeout_s` | 调沙箱执行 Python/JS 代码 |
| `webhook_call`  | `WebhookConfig.id` | 可选 `timeout_s` | 按该 WebhookConfig 的 secret 重新算 HMAC 发 POST |
| `object_api`    | `object_type_iri` 或 `step.ref` | 必填 `operation` (`create_object`/`update_object`/`create_link`) | 直接 mutate Object/Link 表 |
| `delay`         | — | 必填 `seconds` (>=0) | `asyncio.sleep` |

step dict 完整示例：

```json
{
  "id": "send-email",
  "name": "Send welcome email",
  "type": "function_call",
  "ref": "<action_type_id>",
  "input_mapping": {"to": "$trigger.payload.email", "name": "$input.name"},
  "config": {"timeout_s": 30},
  "retry_policy": {"max_attempts": 3, "delay_s": 1},
  "error_handler": "stop"
}
```

`error_handler` 取值：
- `"stop"`（默认）— step 失败 → execution FAILED
- `"continue"` — 跳过失败，下一步继续
- `{"goto_step": "step-3"}` — 跳到指定 step（之间所有 step 标记 SKIPPED）

`retry_policy.max_attempts` 用指数退避 `delay_s * 2 ** (attempt-1)`。

### 25.3 Context 传递：`$prev` / `$steps.<id>` / `$input` / `$trigger`

每个 step 的输出都汇入同一个 context dict，下一个 step 通过 `input_mapping` 引用：

```python
{"to": "$prev.email"}                      # 上一步的整个 output
{"value": "$steps.double.value"}            # 命名 step 的 output
{"name": "$input.name"}                     # 用户调用 /execute 时传入的 input
{"headers": "$trigger.webhook.headers"}     # trigger 上下文
```

`resolve_input_mapping()` 在 `src/runtime/workflow_step_handlers.py`：
- 仅识别以 `$` 开头的 token；其他值原样保留
- 字典递归；标量原样返回
- 路径不存在的回退原占位符（不报错，便于模板调试）

### 25.4 Trigger 集成（关键扩展点）

`trigger_configs` 表加 `workflow_id` 列（nullable，FK → workflows.id）。
触发器 API 现在要求 `action_type_id` 与 `workflow_id` **二选一**（model-level XOR validator）。

`process_inbound_webhook` / `_fire_schedule_trigger` 在 dispatch 时：
- `action_type_id` 非空 → 走原有 `ActionRun` 路径
- `workflow_id` 非空 → 创建 `WorkflowExecution`，调用 `execute_workflow(id, trigger_kind="webhook"|"schedule")`

**意味着 HIA-75 acceptance 用例**（"新客户注册 → 发邮件 → 同步 Salesforce → 创建跟进任务"）
现在可以一步完成：定义一个 `kind="function"` 的 ActionType + 一个 `kind="webhook"` 的 WebhookConfig（Salesforce URL）+ 一个 `object_api` step，配成 Workflow，再用 inbound_webhook trigger 串起来。

### 25.5 ⚠️ SQLite writer-lock + 同一 session 共享

**症状**：HIA-76 第一版 `execute_workflow` 在 sync HTTP 路径下，每个 step 都
`async with async_session_factory() as session` 打开新 session，结果：

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
```

**根因**：endpoint 的 session 还没 commit（外层事务持有写锁），
executor 内部又开 session 写 `workflow_step_results` —— SQLite 序列化锁冲突。

**修复**：
- `execute_workflow(..., session=...)` 接受外部 session；sync HTTP 路径
  把 `get_session()` 返回的 session 直接传进去，整个执行在一个事务里。
- 调用方负责 `await session.commit()`（endpoint 已加）。
- async background 路径不传 session，executor 自己 `async_session_factory()`
  开新 session。

**生产 PG 不踩**：PG 用 MVCC，跨 session 写不冲突。但即便如此，单事务也是
最佳实践（一致性 + 减少 round-trip）。

### 25.6 Background execution 在 ASGITransport 测试下不跑

**症状**：`async_run=true` + `asyncio.create_task(execute_workflow(...))` 在
`httpx.ASGITransport` 下 background task 不执行，execution 永远 PENDING。

**根因**：ASGITransport 的 lifespan 关闭后 background tasks 不会被 drain。

**验证**：用 `lifespan="on"` + `async with AsyncClient(...)` 跨多请求可触发
background task（httpx 维护 ASGI app 生命周期）；或直接调 `execute_workflow`
同步路径绕过。

**生产**: `uvicorn` 跑的多 worker 进程下 background task 正常工作。

### 25.7 验收脚本

参见 `apps/api/tests/test_workflow.py`：CRUD + 顺序执行 + retry + continue
handler + inbound_webhook trigger 集成，共 13 个用例。

### 25.8 AsyncSession owned-session 必须显式 commit

**症状**：executor 后台任务跑完后，DB 里 `execution.status` 还是 PENDING、
`step_result` 行不存在；日志显示 executor 正常完成。

**根因**：`async with async_session_factory() as session` 上下文退出时**不会
自动 commit**。`session.flush()` 把改动推到 connection 缓冲区，但没
`commit()` 的话退出时会回滚到上一次 commit 后的状态。

**FastAPI `get_session` 依赖会代为 commit**（见 `src/db/connection.py`），
但服务层/后台 task 自己 `async_session_factory() as session` 时没人替
你 commit。

**修复模板**：

```python
async def run_owned_session_work(execution_id: ...):
    owns_session = session is None
    if owns_session:
        session = async_session_factory()
    try:
        ... # 业务逻辑 + session.flush()
        if owns_session:
            await session.commit()
        return result
    except Exception:
        if owns_session and session is not None:
            try:
                await session.rollback()
            except Exception:
                pass
        raise
    finally:
        if owns_session:
            await session.__aexit__(None, None, None)
```

`workflow_executor.py::execute_workflow` 走的就是这个模式（owned_session
分支），`async_run=true` 的 endpoint + webhook dispatcher 派发的后台 task
都依赖它落盘；共享 session 路径（`/execute` sync 端点显式传 `session=`）
由调用方 commit，executor 不重复 commit。

### 25.9 后台 task 与手动 execute 的并发去重

**症状**：`test_inbound_webhook_triggers_workflow` / `test_async_run_returns_pending`
单独跑都通过；批量跑偶发 2 个 step_result 或 LookupError。

**根因**：ASGITransport 下 §25.6 让 background task 是否执行变得不确定。
在 race window 内可能：
1. 测试 GET 读到的 status 还是 PENDING → 测试手动 `await execute_workflow`
2. 但 background task 紧接着也跑了 → 两个 invocation 都写 step_result，
   重复

或反向：GET 看到 status=success 但其实 background task 的 commit 还没
replication 完 → 测试手动调用查到 LookupError。

**处理模式**（用在 ASGITransport 测试里）：

```python
# 1. 触发 webhook / async_run
r = await client.post(...)

# 2. 先 GET 一遍把 POST 的事务强制 commit + 拿到最新状态
r = await client.get(f"/workflow-executions/{eid}")
if r.json()["status"] in {"pending", "running"}:
    # 3. 仅在尚未完成时手动驱动 executor
    await execute_workflow(uuid.UUID(eid), ...)

# 4. 断言 step_result 用 dedupe by step_id（容忍 0/1/2 行）
sr = (await client.get(f"/workflow-executions/{eid}/step-results")).json()
step_ids = {row["step_id"] for row in sr}
assert expected_step_id in step_ids
assert all(row["status"] == "success" for row in sr)
```

**注意**：批量测试仍有 CLAUDE.md 第 1 条记录的 `reinit_engines` 时序问题
（LookupError 在第二个测试出现），与本 pitfall 无关，不要混在一起追。

---

## 26. SSO / Identity Provider（HIA-79 / D2）

企业级 SSO 配置 + OIDC 授权码（PKCE）登录 + JIT 用户配置。SAML/LDAP
在 D2 仅做 schema 占位（实际登录流留到 D2.x）。

### 26.1 模块边界

| 模块 | 职责 |
| --- | --- |
| `apps/api/src/db/sso.py` | `IdentityProvider`、`SsoLoginSession` ORM + `is_expired` helper |
| `apps/api/src/services/sso_client.py` | OIDC discovery cache、JWKS cache、PKCE、auth URL builder、code→token、ID-token verify |
| `apps/api/src/api/sso.py` | `provider_router`（CRUD + test）+ `login_router`（login/callback） |
| `apps/api/alembic/versions/2026_09_19_0011_sso.py` | 两张表的迁移 |

### 26.2 数据模型

```python
class IdentityProvider(Base, UUIDMixin, TimestampMixin):
    workspace_id, name, protocol, status
    config: JSON              # {"issuer_url", "client_id", "client_secret", ...}
    claim_mapping: JSON       # {"email": "email", "display_name": "name"}
    secret_fields: JSON       # ["client_secret"]
    auto_provision: bool
    force_sso: bool
    last_test_status / message / at
    created_by

    __table_args__ = (UniqueConstraint("workspace_id", "protocol"), ...)
```

唯一约束 `uq_identity_providers_workspace_protocol` 让一个 workspace
**每种协议最多一个 IdP**（要换 IdP 先 disable 旧的）。

`SsoLoginSession` 跟踪 in-flight flow：`state`（CSRF）/ `nonce`（OIDC
ID-token）/ `code_verifier`（PKCE）/ `redirect_uri` / `relay_state`
（已 sanitize 过的 return_to）。10 分钟 TTL，`consumed_at` 标记单次
使用。

### 26.3 加密 / Mask 约定

- **加密**：用 `src.core.secrets.encrypt_value`（Fernet，带 `enc:v1:` 前缀）。
- **Mask**：API 返回前 `mask_secret_fields(config, idp.secret_fields)`，
  把所有声明的 secret 字段替换成 `"***"`。
- **默认 `secret_fields = ["client_secret"]`**：和 OIDC spec 字段名一致；
  不要造 `client_secret_enc` 别名（详见 §6.37）。
- **解密**：callback 里 `decrypt_value(plain_cfg[f])` 逐字段解；不在 DB
  里直接读 secret 原文。

### 26.4 OIDC 流程（RFC 6749 + RFC 7636 + OpenID Connect Core）

```
┌────────────┐    GET /api/sso/{slug}/login?return_to=...
│  Browser   │ ─────────────────────────────────────────► Backend
│            │ ◄─ 302 /api/sso/callback                   │
│            │ ─► GET /authorize?...&code_challenge=...    │ IdP
│            │ ◄─ 302 /api/sso/callback?code=...&state=... │
│            │ ─► GET /api/sso/callback?...               │ Backend
└────────────┘                                            │
                                                          ▼
                                                  token exchange
                                                  ID-token verify
                                                  JIT user
                                                  JWT issued
                                                  302 /return_to?token=...
```

**关键不变量**：

1. `state` 单次使用 + 短期 TTL（10 min）—— callback 命中后立刻写
   `consumed_at`，replay 会 400。
2. `nonce` 在 ID-token 验证时强制比对，防重放。
3. PKCE `code_verifier` 从未离开 server；authorize 请求只带
   `code_challenge`（SHA-256(verifier)）。
4. `relay_state` 在 login 入口就 sanitize 一次（§6.38），不再依赖
   callback 兜底。

### 26.5 API 契约

| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/workspaces/{wid}/sso/providers` | `POST` | workspace ADMIN | 创建 IdP（自动 encrypt secret） |
| 同上 | `GET` | workspace MEMBER+ | 列表（mask 后返回） |
| `/api/workspaces/{wid}/sso/providers/{pid}` | `GET` / `PATCH` / `DELETE` | GET:MEMBER+ / PATCH,DELETE:ADMIN | 单个操作 |
| `/api/workspaces/{wid}/sso/providers/{pid}/test` | `POST` | workspace ADMIN | test_connection：OIDC 跑 discovery |
| `/api/sso/{slug}/login` | `GET` | 公开 | 302 → IdP authorize URL |
| `/api/sso/callback` | `GET` | 公开 | IdP 回调入口；成功 → `return_to?token=...` |

错误码：

- `user_not_provisioned`：`auto_provision=false` + IdP 返回未知 email
- `discovery_unreachable` / `discovery_failed` / `discovery_invalid_json`
- `token_exchange_failed` / `id_token_invalid` / `id_token_nonce_mismatch`
- 错误一律通过 redirect 的 query string 回传（`sso_error=...&sso_error_message=...`），
  不会泄漏 secret / private key material。

### 26.6 强制 SSO（force_sso）

workspace 的 IdP 设 `force_sso=true` 时，callback 成功后**立刻清掉
该用户的 `password_hash`**，让本地密码登录不可用。bootstrap admin
（`admin@ontolohub.local`）豁免，用于恢复 tenant。

注意：

- 只清 **当前 workspace 的 IdP 登录过的用户**。用户在别的 workspace 没
  走过 SSO 时不动。
- 不影响其他认证方式（API Key / JWT access token 在有效期内仍可用，
  直到 token 自然过期）。

### 26.7 避坑

- §6.34 `from x import func` 失效 — `sso.py` 通过 `sso_client_mod.X()`
  调用 OIDC client，保证测试能 monkeypatch。
- §6.35 SQLite DateTime 列丢 tz — `is_expired` 里 normalize 后比较。
- §6.36 `async_session_factory()` 不自动 commit — 测试 fixture 改了
  row 必须 `await s.commit()`。
- §6.37 加密字段名 = OIDC spec 字段名 — 别名只会让契约对不齐。
- §6.38 open-redirect 在 login 入口就 sanitize。

### 26.8 测试覆盖清单

参见 `apps/api/tests/test_sso.py`：

- Provider CRUD（admin-only、secret 加密、mask、唯一性）
- 工作空间隔离（非成员 404）
- OIDC helper 单测（PKCE shape、auth URL builder）
- 完整 callback flow（JIT 创 user、加入 workspace、issue JWT）
- 安全负例：replay / expired / `auto_provision=false` / force_sso 清密码 /
  外部 `return_to` sanitize

共 19 个用例，全部走 ASGI transport + monkeypatch OIDC client，避免打
真实 IdP。




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

- 认证：调用方传 `X-User-Email`（优先）或 `X-User-Id` header；都缺则 fallback 到 bootstrap admin。**M1 不引入 JWT / OIDC**。
- 授权：每个项目级端点必须挂 `require_role(MIN_ROLE)`；OWNER 才能写成员，EDITOR 才能写业务对象，VIEWER 只能读。
- 隔离：路径中拿到 `project_id` 后，**所有查询必须再 WHERE `project_id == ?`**；helper `_load_xxx_for_project(session, id=..., project_id=...)` 强制这件事，禁止裸用 `select(Foo).where(Foo.id == id)`。
- 失败语义：**不足权限 → 404，不返 403**。这是 M1-10 退出条件，避免 "项目是否存在" 侧信道泄漏。
- 审计：每个**变更**端点（POST / PATCH / DELETE）必须 `await record_audit(...)`，写 before/after + 哈希链；读端点不写。
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

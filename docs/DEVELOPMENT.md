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
- [ ] 没把 `datetime.utcnow()` / `func.now()` 用在审计时间上？
- [ ] 没把 ORM 实例直接当 Pydantic 返回？
- [ ] 改动没破坏 §1–§5 任何一条？

---

## 9. 文档维护

- 这份文件本身有错、或遇到新坑没写进来 → **直接改**；不要在 PR 评论里口头说。
- 改了约定但没更新本文件 → 评审时会被打回。
- 命名 / 路径 / 工具变更，先改本文件再改代码。

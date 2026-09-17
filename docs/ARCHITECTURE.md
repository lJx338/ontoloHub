# OntoloHub 架构

> A1 阶段成稿。随着 Linear M0 → M3 推进会持续更新。

## 一图概览

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Browser (Web)                               │
│                  React + Vite + Tailwind (:3000)                     │
└──────────────────────────┬───────────────────────────────────────────┘
                           │  /api/* proxy
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       FastAPI (:8000)                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐ │
│  │ Projects │  │Evidence  │  │Ontology  │  │ Verification / SHACL │ │
│  │  CRUD    │  │ Inbox    │  │  Studio  │  │                      │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘ │
│  ┌────┴─────┐  ┌────┴─────┐  ┌────┴─────┐  ┌──────────┴───────────┐ │
│  │ Mapping  │  │ Change   │  │ Release  │  │  Object / Query API  │ │
│  │ Engine   │  │ Request  │  │ & Deploy │  │                      │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘ │
│       └──────────────┴─────────────┴────────────────────┘             │
│                              │                                       │
│                  ┌───────────┴────────────┐                          │
│                  │  Domain Service Layer  │                          │
│                  │  (orchestration / use  │                          │
│                  │   cases / transactions)│                          │
│                  └───────────┬────────────┘                          │
│                              │                                       │
│                  ┌───────────┴────────────┐                          │
│                  │     SQLAlchemy 2 ORM    │                          │
│                  └───────────┬────────────┘                          │
└──────────────────────────────┼───────────────────────────────────────┘
                               │
                  ┌────────────┴─────────────┐
                  │     SQLite (M0) /        │
                  │   PostgreSQL (M1+)       │
                  └──────────────────────────┘
```

## 分层

```
apps/api/src/
├── api/             # FastAPI 路由层（HTTP 边界）
│   ├── projects.py
│   ├── evidence.py
│   ├── ontologies.py
│   ├── change_requests.py
│   └── ...
├── services/         # 用例编排层（多步事务 / 跨模块流程）
│   └── ...
├── db/              # 数据访问层
│   ├── base.py      # DeclarativeBase + Mixins
│   ├── connection.py
│   ├── project.py
│   └── ...
├── core/            # 配置 / 错误 / 横切关注
│   ├── config.py
│   ├── errors.py
│   └── reference.py
├── plugins/         # 运行时扩展（Action/Function，M2）
└── utils/
```

**原则：**
- `api/` 只做 HTTP 协议翻译（请求校验 → 调 service → 返回响应）。
- 业务规则都在 `services/` 与 `db/`。
- 跨模块流程（"创建项目 → 上传 evidence → 触发剖析"）写在 `services/`。
- `db/` 不依赖 `api/` 或 `services/`，便于测试与换数据库。

## 核心数据模型

详见 [`docs/data-model.md`](data-model.md)（TBD — A2 落稿后填）。

简化版（首期 MVP）：

```
Project (1) ── (N) OntologyVersion (1) ── (N) OntologyClass / Property / Relation
   │                       │
   │                       └── (N) Constraint (SHACL shape 模板)
   │
   ├── (N) Evidence            ← CSV / XLSX / PG sample
   │       └── (N) Profile     ← 自动剖析结果
   │
   ├── (N) Candidate           ← 字段 → 本体属性的候选映射
   │
   ├── (N) ChangeRequest       ← 改动 PR
   │
   ├── (N) Release             ← 已发布的本体版本快照
   │       └── (N) Deployment
   │
   └── (N) Object              ← 业务对象实例
           └── (N) Link         ← 对象间关系
```

每个领域实体都有：

- `id: UUID`（跨方言：PG native / SQLite CHAR(36)）
- `project_id: UUID`（租户隔离单元 — M0 单租户 → M3 多租户升级为 workspace）
- `created_at / updated_at`
- 软删除：`deleted_at, deleted_by`（`SoftDeleteMixin`）

## 配置与运行环境

| 维度 | 选择 | 备注 |
|---|---|---|
| 后端语言 | Python 3.11+ | FastAPI / Pydantic v2 / SQLAlchemy 2 异步 |
| 前端 | React 18 + Vite + Tailwind | TS 严格模式 |
| 数据库（M0） | SQLite | 文件：./data/ontolohub.db |
| 数据库（M1） | PostgreSQL 16 | Docker Compose 启 |
| 包管理（Python） | pip + venv（M0） / uv（M1+） | 平滑过渡 |
| 包管理（JS） | npm workspaces | 内置 |
| ORM | SQLAlchemy 2.0+ | `Mapped[T]` 风格 + 跨方言 Uuid |
| 迁移 | Alembic | baseline + autogen |
| 校验 | SHACL（pyshacl，M1 起真实执行） | M0 仅占位 |
| 插件运行时 | 内置 sandbox（M2 起引入） | 当前未启用 |
| CLI | Typer（Python） | 命令：`ontolohub` |
| Lint | Ruff（py）+ ESLint（ts） | mypy 严格度 M0→M3 渐增 |

## 部署形态

- **M0**：单机进程，SQLite 单文件，所有用户共享（单租户，但支持多用户登录会从 M1 起）
- **M1**：Docker Compose（PG + Redis + api + worker + web）
- **M2**：+ worker 节点（异步函数执行）
- **M3**：+ 多租户隔离 + SSO + 审计日志

## 安全 / 合规

- M0：本地开发为主，HTTPS 由反代承担
- M1：API key + JWT；CORS 受限
- M2：Secrets 注入 + 函数沙箱
- M3：SSO（OIDC/SAML/LDAP）+ 审计日志 + GDPR/SOC2 导出

## 性能预算（目标值）

- API 单请求 P95 < 200 ms（不含外部服务）
- SHACL 校验 10k Object < 30 s
- Ontology CRUD 单租户下 < 50 ms
- Web 首屏交互 < 2 s（本地开发）

## 已知约束

- M0 阶段 ID 用 `Uuid(as_uuid=True)` 跨方言存储。SQLite 上表现为 CHAR(36)。
- M0 不启用多用户：所有 CRUD 不带 actor（actor 默认 None）。
- M0 不启用 SHACL 真实执行，仅 schema 校验；M1 起 pyshacl 接入。
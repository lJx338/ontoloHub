# OntoloHub

**本体工程与语义应用工作台** — 把"读懂客户数据 → 落地业务本体 → 持续校验与发布"做成一条可被制造业 FDE 团队真正用起来的流水线。

> 当前阶段：**Native MVP（M0）** — 原生安装即可启动，单机 SQLite 存储，前后端分离。

---

## 它是什么

一个面向**企业数据 + 业务语义**的小型工作台：

| 模块 | 作用 |
|---|---|
| **Evidence Inbox** | 收 CSV / XLSX / 数据库抽样，字段剖析，落到"证据收件箱"待决策 |
| **Ontology Studio** | 类 / 属性 / 关系 / 约束的可视化建模，支持版本化（OntologyVersion） |
| **Candidate Mapping** | 基于规则 + LLM 给出"字段 → 本体属性"候选映射，FDE 决策通过/驳回 |
| **SHACL Verification** | 用 SHACL 对 Object / 数据快照做合规校验，给出违规清单 |
| **Change Request** | 对本体/映射的改动走 PR 流程：草案 → 评审 → 通过 / 驳回 |
| **Release & Deployment** | 把通过的 OntologyVersion 发布到目标环境（preflight check） |
| **Object API** | 受本体约束的对象 CRUD、关系查询、保存查询 |

完整路线图见 [`docs/roadmap.md`](docs/roadmap.md) 与 Linear 项目 [OntoloHub](https://linear.app/hiatt/project/ontolohub-a82602909d16)。

---

## 仓库结构

```
ontoloHub/
├── apps/
│   ├── api/          # Python · FastAPI 后端
│   ├── web/          # TypeScript · React + Vite 前端
│   └── cli/          # Python · typer 管理 CLI（命令：ontolohub）
├── packages/
│   └── core/         # 跨包共享的 Python 工具
├── scripts/          # 安装、启动、清理、补丁
├── docs/             # 架构说明、API 文档、roadmap
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md     # 开发规范 + 避坑指南（必读）
│   ├── MILESTONES.md
│   └── ...
├── install.ps1       # Windows 原生安装（推荐 PowerShell 5.1+）
├── install.sh        # macOS / Linux 原生安装
├── package.json      # npm workspaces 根
└── pyproject.toml    # Python monorepo 根（metadata）
```

---

## 一键安装

### 前置

- **Python ≥ 3.11**（在 PATH 上能找到 `python` / `python3`）
- **Node.js ≥ 20**（在 PATH 上能找到 `node` / `npm`）
- **Git**（克隆仓库用）

### 安装

**Windows（PowerShell）：**
```powershell
git clone <repo-url> ontoloHub
cd ontoloHub
.\install.ps1            # 默认：venv + pip + npm + 初始化 SQLite
.\install.ps1 -Dev       # 加装 dev 依赖（pytest / ruff / mypy）
.\install.ps1 -Force     # 重建 venv 与 node_modules
```

**macOS / Linux：**
```bash
git clone <repo-url> ontoloHub
cd ontoloHub
chmod +x install.sh
./install.sh             # 默认
./install.sh --dev       # 加装 dev 依赖
./install.sh --force     # 重建 venv 与 node_modules
```

> 安装脚本是幂等的：依赖装过了会跳过；`.env` 已存在不会覆盖。

---

## 启动

激活虚拟环境（如果还没激活），然后：

```bash
npm run dev             # 同时启动 API (:8000) + Web (:3000)
# 或者单独：
npm run dev:api         # 只起 FastAPI
npm run dev:web         # 只起 Vite
```

浏览器打开：

- **Web 工作台**：<http://localhost:3000>
- **API 根**：<http://localhost:8000/>
- **Swagger 文档**：<http://localhost:8000/api/docs>
- **健康检查**：<http://localhost:8000/health>

CLI：
```bash
ontolohub version       # 打印 CLI 与运行时版本
ontolohub doctor        # 环境自检
ontolohub db status     # 测试 DB 连接
ontolohub db migrate    # 运行 Alembic 升级
```

---

## 数据存储

M0 阶段默认 **SQLite**，文件位于 `./data/ontolohub.db`，零外部依赖。

切到 PostgreSQL（M1 阶段会原生开启）：
```env
# .env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/ontolohub
```
然后 `ontolohub db migrate`。

---

## 测试与质量

```bash
# 激活 venv 后：
pytest                  # 后端单元 / 接口测试
ruff check .            # 代码风格
mypy apps packages      # 类型检查

# Web：
npm run lint            # 前端 lint
```

---

## 路线图

详见 [`docs/roadmap.md`](docs/roadmap.md)。

| Milestone | 截止 | 关键能力 |
|---|---|---|
| **M0 — Native MVP** | 2026-10-31 | 单机 SQLite · 端到端本体 CRUD · 证据收件箱 · SHACL 校验 · 变更/发布流 |
| **M1 — Multi-User + Docker** | 2026-12-31 | PG + Redis · 多用户 · PG 连接器 · 真实 SHACL |
| **M2 — Action Functions** | 2027-03-31 | Function 编辑器 · 沙箱运行时 · Workflow 编排 |
| **M3 — Multi-Tenant + Enterprise** | 2027-06-30 | 多租户 · SSO · 审计/合规导出 |

---

## 贡献

读 [`docs/architecture.md`](docs/architecture.md) 了解分层，再读 [`docs/getting-started.md`](docs/getting-started.md) 跟一遍 E2E。

PR 流程：
1. 在 Linear 项目 OntoloHub 下开 issue（或挑一张卡）
2. 拉分支：`git checkout -b liuxiaoge23/HIA-XX-short-name`
3. 改代码 + 加测试
4. `git commit -m "HIA-XX: ..."`
5. 推分支、提 PR

## 许可证

MIT — 见 [`LICENSE`](LICENSE)。
# 路线图

> 与 Linear 项目 [OntoloHub](https://linear.app/hiatt/project/ontolohub-a82602909d16) 同步维护。

## Milestones

| 代号 | 名称 | 截止 | 关键交付 |
|---|---|---|---|
| **M0** | Native MVP | 2026-10-31 | 单机 SQLite、端到端本体 CRUD、Evidence Inbox、SHACL 校验、Change Request、Release |
| **M1** | Multi-User + Docker | 2026-12-31 | PG + Redis + 多用户、PG 只读连接器、完整 SHACL、Connector 框架 |
| **M2** | Action Functions | 2027-03-31 | Function 编辑器、沙箱执行、Workflow 编排、Webhook |
| **M3** | Multi-Tenant + Enterprise | 2027-06-30 | 多租户、SSO、审计/合规导出、版本化发布线 |

## M0 — Native MVP（A1–A18，18 个 issue）

| ID | 标题 | 优先级 |
|---|---|---|
| HIA-50 | A1 项目骨架 + 原生安装脚本 | ⚡ Urgent |
| HIA-51 | A2 Project CRUD API | ⚡ Urgent |
| HIA-49 | A3 Evidence Inbox — CSV/XLSX 上传 | ⚡ Urgent |
| HIA-53 | A4 数据剖析执行 | ⚡ Urgent |
| HIA-52 | A5 候选映射生成（rule-based） | ⚡ Urgent |
| HIA-55 | A6 证据收件箱 API + 决策流 | ⚡ Urgent |
| HIA-56 | A7 Ontology Version + CRUD | ⚡ Urgent |
| HIA-54 | A8 OWL/TTL/JSON-LD 导出 | ⚡ Urgent |
| HIA-58 | A9 SHACL 校验执行 | High |
| HIA-57 | A10 Change Request 工作流 | ⚡ Urgent |
| HIA-61 | A11 Release + Deployment + Preflight | ⚡ Urgent |
| HIA-59 | A12 Object & Link API | ⚡ Urgent |
| HIA-63 | A13 前端 — 骨架 + 路由 + 工作台 | High |
| HIA-62 | A14 前端 — 本体编辑器 | High |
| HIA-60 | A15 前端 — 证据收件箱 | High |
| HIA-68 | A16 前端 — Verification | Medium |
| HIA-65 | A17 前端 — 变更/发布/部署 | Medium |
| HIA-66 | A18 E2E + Seed 数据 | Medium |

## M1 — Multi-User + Docker（B1–B6，6 个 issue）

| ID | 标题 | 优先级 |
|---|---|---|
| HIA-64 | B1 Docker Compose + PG + Redis + 多用户 | Urgent |
| HIA-67 | B2 PostgreSQL 只读连接器 | High |
| HIA-72 | B3 候选映射引擎增强 | High |
| HIA-73 | B4 完整 SHACL 校验执行 | High |
| HIA-69 | B5 CR 工作流增强 | High |
| HIA-71 | B6 Connector 框架 | High |

## M2 — Action Functions（C1–C4，4 个 issue）

| ID | 标题 | 优先级 |
|---|---|---|
| HIA-70 | C1 Action / Function 编辑器 | Urgent |
| HIA-78 | C2 Functions 运行时（Python/JS 沙箱） | Urgent |
| HIA-75 | C3 Webhook / 触发器集成 | High |
| HIA-76 | C4 Action 编排（Workflow） | High |

## M3 — Multi-Tenant + Enterprise（D1–D4，4 个 issue）

| ID | 标题 | 优先级 |
|---|---|---|
| HIA-77 | D1 多租户 | Urgent |
| HIA-79 | D2 SSO（OIDC/SAML/LDAP） | High |
| HIA-80 | D3 审计日志 + 合规导出 | High |
| HIA-74 | D4 版本化发布线 | High |

---

## 优先级（建议执行顺序）

1. **A1 骨架**（HIA-50，本卡）
2. **A2 Project CRUD**（HIA-51）— 后续一切的前置
3. **A3-A5**（HIA-49/53/52）— 收件箱 → 剖析 → 候选的流水线
4. **A6-A8**（HIA-55/56/54）— 决策 → 本体 CRUD → 导出
5. **A9-A12**（HIA-58/57/61/59）— 校验、CR、Release、Object
6. **A13-A17**（HIA-63/62/60/68/65）— 前端工作台
7. **A18**（HIA-66）— E2E + seed
8. M1 → M2 → M3 逐级推进
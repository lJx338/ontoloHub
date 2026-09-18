# CLAUDE.md

> AI Agent / Claude 在 OntoloHub 项目工作时必读。完整规范见 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。

## 项目概览

OntoloHub 是本体驱动的数据建模平台：FastAPI 后端 + SQLAlchemy 异步 + SQLite/PostgreSQL + Alembic。

- **后端目录**：`apps/api/`
- **DB 模型**：`apps/api/src/db/`
- **路由**：`apps/api/src/api/`
- **测试**：`apps/api/tests/`
- **M0/M1/M2/M3 milestone 对应**：A1-A19 / B1-B9 / C1-C9 / D1-D9

## 开发循环

```bash
cd apps/api
# 改完代码后：
python -m pytest tests/test_xxx.py -v          # 单文件
python -m pytest tests/ --tb=short 2>&1 | tail  # 全量（耗时 ~5min）
git add <files> && git commit -m "<type>(<scope>): <subject>"
```

## 关键陷阱（必读）

### 1. 测试 isolation 偶发失败
批量跑测试偶发 1-2 个 IntegrityError，**单独跑总是通过**——这是已知环境问题（`reinit_engines` 时序），不是代码 bug。重跑即可，**不要花时间追**。

### 2. 异步 ORM 必须 refresh
```python
session.add(obj); await session.flush(); await session.refresh(obj)
```
否则 `created_at` / `updated_at` 等数据库默认值为 None。

### 3. JSON 列直接传 list/dict
不要 `json.dumps()`，SQLAlchemy 会处理。

### 4. Pydantic 枚举字段
- 响应模型：传 `EnumClass.XXX` 实例
- DB 模型：传 `EnumClass.XXX` 实例
- 不要传字符串（SQLAlchemy 不会自动转，FastAPI 才会）

### 5. UUID 字段
Pydantic 模型用 `uuid.UUID`，FastAPI 自动转换；DB 用 `UUID(as_uuid=True)`。

### 6. 决策流 / 接受 proposal
参考 `apps/api/src/api/proposals.py::create_decision` 的 HIA-72 B3 模式：flush decision → 写 evidence 确认 → 写 AuditEvent（append-only）。

### 7. git push 网络限制
当前开发机可能不可达 github.com。**本地 commit 可以，push 必须用户手动触发**。

## 路由命名约定

- 资源 CRUD：`POST /<resource>`、`GET /<resource>/{id}`
- 子资源挂在父资源：`POST /ontologies/{id}/classes`
- 版本/快照：`/versions/{vid}/...`
- 项目前缀：`/projects/{project_id}/...` 用于跨资源聚合

## 响应模型

每个 endpoint 都声明 `response_model=...`，返回前用 `_to_response(obj)` 显式构造（避免 ORM lazy-load 触发意外查询）。

## 提交格式

```
<type>(<scope>): <subject>  [中文]

<可选 body>
```
type: feat / fix / docs / refactor / test / chore

## Linear 流程

- 分支：`<owner>/<issue-id>-<代号-英文简写>`
- Issue ID：`HIA-NN`
- 完成任务后调用 `plugin_linear_linear_save_issue` 将状态改为 "Done"

## 优先任务（按 M0 顺序）

1. ~~HIA-61 (A11) Release/Deployment/Preflight~~ ✅ Done
2. **HIA-56 (A7) Ontology Version + CRUD** ← 当前
3. HIA-57 (A10) Change Request 工作流
4. HIA-58 (A9) SHACL 校验执行
5. HIA-59 (A12) Object & Link API

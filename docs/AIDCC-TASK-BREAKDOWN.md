# AIDCC 执行任务拆分

规划版本：v3｜控制面快照：2026-09-08｜状态：全部 DRAFT，尚未执行

本文件将 [PRD](PRD.md) 与 [MILESTONES](MILESTONES.md) 的已确认范围映射到 AIDCC 的当前任务拆分，作为 Git 中可审查的执行记录。产品范围、阶段退出条件与取舍规则仍以 `MILESTONES.md` 为唯一基准；本文件不替代它们，也不代表任务已经开工。

## 1. 当前控制面状态

- 规划版本：v3（`e5b6ada4-15a4-430b-b167-a1f476122423`）。
- 阶段：M0–M4 均为 `PLANNED`；全部 `startAfter=[]`，允许并行开发。
- 阶段验收顺序：M0 无前置；M1 完成后置于 M0；M2 后置于 M1；M3 后置于 M2；M4 后置于 M3。
- 任务：23 个，全部为 `DRAFT`；均已绑定主仓库、默认本地环境和 `main` 基线，未分配 Worker。
- 风险：除 M0 用例/架构走查为 `LOW` 外，其余均为 `MEDIUM`；不以降低风险规避实际控制要求。
- 路径：各任务的允许修改范围已去重；公共契约、迁移和共享模型保持串行依赖。

## 2. 阶段任务分布

| 阶段 | 任务数 | 交付焦点 |
|---|---:|---|
| M0 技术与产品闸门 | 5 | 用例、稳定契约、Cordis/Semantica 技术验证、存储与基准 |
| M1 FDE内部MVP | 6 | 项目隔离、资料接入、本体/映射、FDE 工作台、重放 |
| M2 对象应用MVP+ | 4 | ObjectView、三模板、动作沙箱、OpenMetadata、能力包 |
| M3 客户试点 | 6 | 客户源映射、同步/漂移、内部任务、权限、离线交付、观察 |
| M4 受控生产基线 | 2 | 恢复/兼容/性能与第二项目复制、正式验收 |

## 3. 工作包

### M0：技术与产品闸门

| ID | 工作包 | 依赖 | 允许修改范围 | 验收摘要 |
|---|---|---|---|---|
| `41c1916a` | 质量追溯用例、架构基线与 FDE 走查 | — | 根配置、`docs/architecture/**`、`docs/use-cases/**`、`docs/prototypes/**`、`scripts/bootstrap/**` | 记录关键实体/关系和三条人工标定查询；完成 FDE 走查、ADR、风险记录和开发骨架。 |
| `a54e6686` | 冻结 Project/Evidence/Version/Mapping/Release 基础契约 | `41c1916a` | `packages/contracts/**` | 稳定 ID、版本、错误约定与 View/Action 边界；覆盖兼容和非法状态。 |
| `4b82bf14` | 验证 Cordis 插件加载、卸载与跨进程调用 | `a54e6686` | `packages/plugin-runtime/**`、`packages/plugin-runtime-testkit/**` | 验证缺依赖、卸载清理、在途任务取消、超时与可恢复错误。 |
| `e5dbd31f` | 验证单图存储、持久任务与性能基准方案 | `a54e6686` | `apps/api/src/persistence/**`、`apps/api/src/db/**`、`apps/api/migrations/**`、`docs/benchmarks/**` | 记录存储/模型限制；最小迁移覆盖隔离与并发；固定样本可重放。 |
| `be035dfe` | 验证 Semantica 支持子集与语义本体边界 | `e5dbd31f`、`4b82bf14` | `packages/semantic-adapter/**`、`packages/semantic-test-fixtures/**`、`docs/semantics/**` | CSV 到审定快照、映射、SHACL、Turtle/JSON-LD；不支持构造须显式报错。 |

### M1：FDE内部MVP

| ID | 工作包 | 依赖 | 允许修改范围 | 验收摘要 |
|---|---|---|---|---|
| `491573df` | 项目、成员、角色、需求与基础审计 API | `e5dbd31f` | `apps/api/src/http/**`、`apps/api/src/application/**`、`apps/api/test/http/**` | 工作区隔离、授权、审计、乐观并发和跨项目负例。 |
| `39f9bb4f` | 证据接入、候选、映射与本体发布流 | `be035dfe`、`491573df` | `packages/ontology-ingestion/**`、`packages/ontology-mapping-fixtures/**`、`packages/source-discovery/**` | CSV/XLSX、文本和只读 PostgreSQL；候选审核、映射、SHACL 与可定位失败。 |
| `c311ecfc` | FDE 内部 MVP 工作台 | `491573df`、`39f9bb4f` | `apps/web/src/app/**`、`apps/web/src/features/fde-workbench/**`、`apps/web/src/features/evidence/**`、`apps/web/src/features/ontology/**` | 两名非开发 FDE 能独立完成固定项目全流程与三条查询。 |
| `bd7cc339` | 受控候选生成与人工审核 | `be035dfe`、`39f9bb4f` | `packages/copilot-runtime/**`、`packages/copilot-evals/**` | 单模型候选附来源与置信度；可审核、拒绝且不自动发布。 |
| `4104f3d1` | 锁版交付包、重放与 Compose preflight | `39f9bb4f`、`491573df` | `packages/export-runtime/**`、`examples/customer-delivery/**`、`docs/runbooks/m1-delivery/**` | 制品在干净环境重放；项目重新绑定、secret 外置和 preflight 阻断。 |
| `f9546300` | 独立 FDE 演练、负例与交付重放 | `c311ecfc`、`4104f3d1` | `tests/fde-acceptance/**`、`examples/quality-traceability/**`、`docs/acceptance/m1/**` | 两名 FDE 的独立演练、权限/故障负例、缺陷记录和重放证据。 |

### M2：对象应用MVP+

| ID | 工作包 | 依赖 | 允许修改范围 | 验收摘要 |
|---|---|---|---|---|
| `f4f0c459` | ObjectView、三模板与 FDE 演示体验 | `a54e6686`、`491573df` | `packages/object-view-runtime/**`、`apps/web/src/features/object-views/**`、`apps/web/e2e/object-views/**` | 设备详情、质量收件箱、审核表单复用组件与合同。 |
| `37e5ad80` | ActionType 沙箱动作闭环 | `a54e6686`、`4b82bf14`、`491573df` | `packages/action-runtime/**`、`packages/action-sandbox-fixtures/**` | 一条沙箱动作链；前置校验、幂等、权限与审计。 |
| `47f49a20` | OpenMetadata 只读目录适配与数据关联 | `4b82bf14`、`491573df` | `packages/plugins/openmetadata/**` | 可选只读字段、源标识和标签适配；不提供完整血缘或治理回写。 |
| `653f1b57` | 用例能力包与跨项目配置重放 | `f4f0c459`、`37e5ad80` | `packages/capability-package/**`、`examples/object-app-package/**`、`docs/runbooks/m2-capability-package/**` | 锁定视图/动作/能力映射，在新项目重放并保持项目隔离。 |

### M3：客户试点

| ID | 工作包 | 依赖 | 允许修改范围 | 验收摘要 |
|---|---|---|---|---|
| `19ea4ae6` | 客户有限源映射与只读 MES/ERP 适配 | `39f9bb4f` | `packages/customer-connectors/**`、`packages/customer-mapping/**`、`examples/customer-source-contracts/**` | 两类获准源、限定实体与字段、只读映射及关键关系对账。 |
| `37d942b1` | 批同步、漂移提案与运行诊断 | `4b82bf14`、`491573df`、`37e5ad80` | `packages/sync-runtime/**`、`packages/observability/**`、`docs/runbooks/observability/**` | 检查点、去重、漂移提案、影响/新鲜度/失败诊断。 |
| `518dace0` | 工厂/产线范围、属性遮蔽与权限解释 | `a54e6686`、`491573df` | `packages/governance-runtime/**`、`packages/governance-test-fixtures/**` | 对列表、搜索、聚合、导出、后台任务和制品的细粒度权限验证。 |
| `46a33622` | 内部任务、备注与受限自动化通知 | `37e5ad80`、`518dace0` | `packages/internal-task-runtime/**`、`packages/notification-runtime/**`、`packages/internal-task-fixtures/**` | 内部待办、站内通知、去重、暂停、恢复与审计；无外部写回。 |
| `f1634d77` | 客户用例包、离线 Compose 与升级切换手册 | `39f9bb4f`、`f4f0c459`、`37e5ad80` | `infra/templates/**`、`docs/runbooks/deployment/**`、`.github/workflows/**` | 断网安装、环境检查、一次升级、版本切换与故障定位。 |
| `a4efb146` | 客户演练、关键关系对账与两周观察 | `19ea4ae6`、`46a33622`、`37d942b1`、`f1634d77` | `tests/customer-pilot/**`、`examples/customer-pilot/**`、`docs/pilot-evidence/**` | 客户用例和查询确认、100 条抽检、缺陷闭环及连续两周观察。 |

### M4：受控生产基线

| ID | 工作包 | 依赖 | 允许修改范围 | 验收摘要 |
|---|---|---|---|---|
| `50388dcf` | 恢复、兼容、安全与固定基准质量门禁 | `f4f0c459`、`37e5ad80`、`518dace0`、`37d942b1`、`f1634d77` | `packages/recovery-runtime/**`、`packages/compatibility/**`、`packages/performance-harness/**`、`tests/security/**`、`tests/performance/**`、`scripts/quality/**` | RPO/RTO、插件白名单、兼容矩阵与固定基准性能门槛。 |
| `963960ff` | 第二项目复制、正式验收与发布基线 | `50388dcf` | `tests/integration/**`、`examples/manufacturing-uat/**`、`docs/acceptance/m4/**`、`docs/runbooks/support/**` | 核心代码零客户分叉的第二项目复制、支持边界、正式验收和交接。 |

## 4. 调度与外部条件

任务被置为 `READY` 前应重新执行 AIDCC 调度校验。当前阶段与任务的结构性依赖已经满足规划要求，但以下条件是外部或控制面门槛：

1. Worker 必须被显式授权访问 ontoloHub；未授权 Worker 不领取项目任务。
2. 任务开始前须存在通过校验的项目配置报告，并按实际执行器注册 Agent Profile 与 Skill。
3. M3 的客户数据、网络、只读访问、授权边界和验收窗口必须按 `MILESTONES.md` 第 3 节到位；Mock 结果不得替代客户验收。
4. 所有生产发布、客户凭据配置、权限授予和受保护分支合并，继续通过独立的人为授权与审查流程执行。

## 5. 变更记录

- v2：将既有 18 个任务按 `MILESTONES.md` 重新归属、收窄路径，并建立 M0–M4 阶段验收链。
- v3：补齐 M1 独立 FDE 演练、M2 能力包重放，以及 M3 客户有限源适配、内部任务/通知、客户观察五项文档明确的工作包；所有任务保持 DRAFT。

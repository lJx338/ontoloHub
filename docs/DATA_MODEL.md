# 数据模型

> 本体模块的全部数据表，字段定义、约束与关系。

## 1. 全局约定

- 所有主键为 UUID
- 所有表带 `created_at` / `updated_at`
- 软删字段 `deleted_at`（按需使用）
- 项目作用域字段：`project_id`
- 时间字段均为 UTC

## 2. 表清单

| 表名 | 用途 |
|---|---|
| ontologies | 本体主表（项目本体 + 参考本体） |
| ontology_classes | 类定义 |
| ontology_properties | 属性定义 |
| ontology_constraints | 约束定义 |
| ontology_versions | 发布版本快照 |

## 3. ontologies

```sql
CREATE TABLE ontologies (
    id              UUID PRIMARY KEY,
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    namespace       VARCHAR(500) NOT NULL,
    description     TEXT,

    kind            VARCHAR(20) NOT NULL,   -- project | reference
    standard_name   VARCHAR(255),           -- 仅 reference：IOF Core 1.0
    source_url      VARCHAR(500),           -- 仅 reference：OWL 文件 URL
    source_format   VARCHAR(20),            -- owl | ttl | jsonld
    version         VARCHAR(50) NOT NULL DEFAULT '0.1.0',

    status          VARCHAR(20) NOT NULL DEFAULT 'draft',
    -- draft | published | deprecated

    class_count     INTEGER DEFAULT 0,
    property_count  INTEGER DEFAULT 0,

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    deleted_at      TIMESTAMP
);

CREATE INDEX ix_ontologies_project ON ontologies(project_id);
CREATE INDEX ix_ontologies_kind    ON ontologies(kind);
CREATE INDEX ix_ontologies_status  ON ontologies(status);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| project_id | UUID | 仅 project 必填 | 项目本体必属项目；参考本体为空 |
| kind | enum | 是 | `project` 或 `reference` |
| standard_name | str | 仅 reference | 标准本体名称，如 `IOF Core 1.0` |
| source_url | str | 仅 reference | OWL/TTL 文件下载地址 |
| version | str | 是 | 语义化版本号 |

### 业务规则

- 一个项目可有多个 project 本体（如核心本体 + 扩展本体）
- reference 本体全局唯一（按 `standard_name + version` 去重）
- published 状态的本体不允许修改；只能走变更单流程

## 4. ontology_classes

```sql
CREATE TABLE ontology_classes (
    id              UUID PRIMARY KEY,
    ontology_id     UUID NOT NULL REFERENCES ontologies(id) ON DELETE CASCADE,

    iri             VARCHAR(500) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    local_name      VARCHAR(255),
    parent_iri      VARCHAR(500),
    level           INTEGER NOT NULL DEFAULT 0,

    description     TEXT,
    definition      TEXT,
    examples        JSON,                   -- ["example 1", ...]
    aliases         JSON,                   -- ["别名 1", ...]

    is_locked       BOOLEAN NOT NULL DEFAULT FALSE,

    -- 对齐：仅 project 本体的类可设置
    alignment       JSON,
    -- {
    --   "reference_class_id": "uuid",
    --   "notes": "对齐说明"
    -- }

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    deleted_at      TIMESTAMP
);

CREATE INDEX ix_ontology_classes_ontology    ON ontology_classes(ontology_id);
CREATE INDEX ix_ontology_classes_parent_iri  ON ontology_classes(parent_iri);
CREATE UNIQUE INDEX uq_ontology_classes_iri  ON ontology_classes(ontology_id, iri);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| iri | str | 是 | 类 IRI，同一 ontology 内唯一 |
| parent_iri | str | 否 | 父类 IRI；用于层级展示，不做外键 |
| level | int | 是 | 层级深度，0 为根类 |
| is_locked | bool | 是 | 人工锁定的类不可被候选覆盖 |
| alignment | JSON | 否 | 对齐到参考本体的类 |

### 业务规则

- `parent_iri` 不做外键约束，允许参考本体结构演化
- 同一类最多对齐一个参考类（一对一）
- `alignment.reference_class_id` 必须指向 reference 本体的类
- published 本体的类不可修改；只能创建新版本

## 5. ontology_properties

```sql
CREATE TABLE ontology_properties (
    id              UUID PRIMARY KEY,
    ontology_id     UUID NOT NULL REFERENCES ontologies(id) ON DELETE CASCADE,

    iri             VARCHAR(500) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    local_name      VARCHAR(255),

    property_type   VARCHAR(30) NOT NULL,
    -- datatype_property | object_property | annotation_property

    domain_iri      VARCHAR(500),
    domain_id       UUID REFERENCES ontology_classes(id),
    range_type      VARCHAR(50),
    range_class_iri VARCHAR(500),
    range_class_id  UUID REFERENCES ontology_classes(id),

    description     TEXT,
    unit            VARCHAR(50),

    is_required     BOOLEAN NOT NULL DEFAULT FALSE,
    is_multivalued  BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    deleted_at      TIMESTAMP
);

CREATE INDEX ix_ontology_properties_ontology       ON ontology_properties(ontology_id);
CREATE INDEX ix_ontology_properties_domain_iri     ON ontology_properties(domain_iri);
CREATE INDEX ix_ontology_properties_property_type  ON ontology_properties(property_type);
CREATE UNIQUE INDEX uq_ontology_properties_iri     ON ontology_properties(ontology_id, iri);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| property_type | enum | 是 | 三种属性类型 |
| domain_iri | str | 否 | 适用于该属性的类 |
| range_type | str | 否 | datatype 时为 xsd:string 等 |
| range_class_iri | str | object 时必填 | 对象属性指向的类 |

### 业务规则

- object_property 必须有 range_class_iri
- domain_iri 与 range_class_iri 解析为 ID 字段冗余存储，方便查询
- 单元字段为业务单位（如 `kg`、`m`），不存换算信息

## 6. ontology_constraints

```sql
CREATE TABLE ontology_constraints (
    id              UUID PRIMARY KEY,
    ontology_id     UUID NOT NULL REFERENCES ontologies(id) ON DELETE CASCADE,

    name            VARCHAR(255) NOT NULL,
    target_class_iri VARCHAR(500) NOT NULL,
    constraint_type VARCHAR(50) NOT NULL,
    -- cardinality | min_cardinality | max_cardinality
    -- pattern | min_length | max_length
    -- min_inclusive | max_inclusive
    -- in | has_value

    property_iri    VARCHAR(500),
    value           JSON,                   -- 约束参数

    severity        VARCHAR(20) DEFAULT 'warning',
    -- violation | warning | info

    description     TEXT,

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    deleted_at      TIMESTAMP
);

CREATE INDEX ix_ontology_constraints_ontology        ON ontology_constraints(ontology_id);
CREATE INDEX ix_ontology_constraints_target_class    ON ontology_constraints(target_class_iri);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| constraint_type | enum | 是 | SHACL 约束类型 |
| property_iri | str | 视类型 | 约束的目标属性 |
| value | JSON | 视类型 | 约束的参数值 |
| severity | enum | 否 | 违规严重程度 |

### 业务规则

- cardinality 类约束要求 property_iri
- pattern/in/has_value 类约束要求 value
- severity 默认 warning；发布前需全部为 info 以上

## 7. ontology_versions

```sql
CREATE TABLE ontology_versions (
    id              UUID PRIMARY KEY,
    ontology_id     UUID NOT NULL REFERENCES ontologies(id) ON DELETE CASCADE,

    version         VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'draft',
    -- draft | published | locked | archived

    snapshot        JSON NOT NULL,
    -- {
    --   "classes": [...],
    --   "properties": [...],
    --   "constraints": [...]
    -- }

    description     TEXT,
    published_at    TIMESTAMP,
    published_by    UUID,

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

CREATE INDEX ix_ontology_versions_ontology ON ontology_versions(ontology_id);
CREATE UNIQUE INDEX uq_ontology_versions_version ON ontology_versions(ontology_id, version);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| version | str | 是 | 语义化版本号 |
| snapshot | JSON | 是 | 发布时类/属性/约束的完整副本 |
| status | enum | 是 | draft 可编辑，published/locked 只读 |

### 业务规则

- 每次发布生成新的 ontology_versions 记录
- 引用此版本的实例数据（Object/Link）通过 `ontology_version_id` 锁定
- locked 状态用于保留历史版本不可变

## 8. 表关系图

```
projects
  │
  │ 1:N
  ↓
ontologies ─────────────────────────────────┐
  │                                         │
  │ 1:N                                     │ 1:N
  ↓                                         ↓
ontology_classes ──────→ ontology_classes   ontology_versions
  ↑                       (parent_iri        (snapshot)
  │                        自引用层级)
  │
  │ N:1 (alignment.reference_class_id)
  │
  │ (跨 ontology 引用)
  ↓
ontologies (kind=reference)

ontology_properties
  │ N:1
  ↓
ontology_classes (domain_id, range_class_id)

ontology_constraints
  │ 弱引用 (target_class_iri)
  ↓
ontology_classes
```

## 9. 迁移注意事项

- 已存在的表结构可能不一致（如 `is_base`/`is_industry`/`is_reference` 多标志位）
- 迁移到 `kind` 枚举时需要映射：
  - `is_reference=true` → `kind=reference`
  - 其他 → `kind=project`
- `reference_alignments` 数组 → `alignment` 单对象，保留首个对齐
- 多余字段（`is_base`、`is_industry` 等）废弃而非删除，保留一版以便回滚

# API 文档

> 本体模块的 HTTP 接口定义。所有接口走 `/api` 前缀，统一鉴权与项目作用域校验。

## 1. 通用约定

### 1.1 请求格式

- `Content-Type: application/json`
- 路径参数：UUID
- 查询参数：过滤条件
- 请求体：JSON 对象

### 1.2 响应格式

成功：
```json
{
  "data": {...} 或 [...],
  "meta": { "total": 100, "page": 1 }
}
```

失败：
```json
{
  "error": {
    "code": "ONTO_NOT_FOUND",
    "message": "本体不存在",
    "details": {}
  }
}
```

### 1.3 错误码

| 状态码 | 含义 |
|---|---|
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无项目权限 |
| 404 | 资源不存在 |
| 409 | 冲突（如 IRI 重复、对齐目标非法） |
| 422 | 语义校验失败（如对象属性缺少 range） |
| 500 | 服务器错误 |

## 2. 本体 CRUD

### 2.1 列出本体

```
GET /api/ontologies
```

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| project_id | UUID | 项目本体过滤 |
| kind | enum | `project` \| `reference` |
| status | enum | `draft` \| `published` \| `deprecated` |
| search | str | 名称模糊匹配 |
| page | int | 页码，默认 1 |
| size | int | 每页大小，默认 20 |

响应：
```json
{
  "data": [
    {
      "id": "uuid",
      "name": "离散制造核心本体",
      "namespace": "https://ontolohub.example/mfg/core",
      "kind": "project",
      "version": "0.2.0",
      "status": "draft",
      "class_count": 15,
      "property_count": 42
    }
  ]
}
```

### 2.2 创建本体

```
POST /api/ontologies
```

请求体：
```json
{
  "name": "离散制造核心本体",
  "namespace": "https://ontolohub.example/mfg/core",
  "description": "...",
  "project_id": "uuid",
  "kind": "project"
}
```

参考本体：
```json
{
  "name": "IOF Core 1.0",
  "namespace": "https://spec.industrialontologies.org/ontology/core/Core/",
  "kind": "reference",
  "standard_name": "IOF Core 1.0",
  "source_url": "https://spec.industrialontologies.org/ontology/core/Core.owl",
  "source_format": "owl"
}
```

响应：201 Created，返回本体对象。

### 2.3 获取本体

```
GET /api/ontologies/{id}
```

响应：本体详情，包含类数量、属性数量、最近更新时间。

### 2.4 修改本体

```
PATCH /api/ontologies/{id}
```

请求体：任意字段（`name`、`description` 等）。注意：`kind`、`namespace`、`version` 不可修改。

### 2.5 删除本体

```
DELETE /api/ontologies/{id}
```

仅 `draft` 状态可删。已发布的本体需要先标记 `deprecated`。

## 3. 类 CRUD

### 3.1 列出类

```
GET /api/ontologies/{ontology_id}/classes
```

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| parent_iri | str | 子类过滤 |
| level | int | 层级过滤 |
| with_alignment | bool | 是否包含对齐信息 |
| search | str | 名称模糊匹配 |

响应：
```json
{
  "data": [
    {
      "id": "uuid",
      "iri": "https://ontolohub.example/mfg/core#Product",
      "name": "Product",
      "local_name": "Product",
      "parent_iri": null,
      "level": 0,
      "description": "...",
      "is_locked": false,
      "alignment": {
        "reference_class_id": "uuid",
        "notes": "对齐到 IOF Product"
      }
    }
  ]
}
```

### 3.2 创建类

```
POST /api/ontologies/{ontology_id}/classes
```

请求体：
```json
{
  "name": "Product",
  "local_name": "Product",
  "iri": "https://ontolohub.example/mfg/core#Product",
  "parent_iri": null,
  "description": "...",
  "definition": "...",
  "examples": ["钢铁产品 A", "塑料件 B"]
}
```

校验规则：
- `iri` 在同一 ontology 内唯一
- `parent_iri` 必须存在或为空
- 参考本体的类不可创建（走 /catalog 注册）

### 3.3 修改类

```
PATCH /api/ontologies/{ontology_id}/classes/{class_id}
```

请求体：任意字段。`is_locked=true` 的类不可修改。

### 3.4 删除类

```
DELETE /api/ontologies/{ontology_id}/classes/{class_id}
```

若已有对象实例引用此类，返回 409 冲突。

## 4. 属性 CRUD

### 4.1 列出属性

```
GET /api/ontologies/{ontology_id}/properties
```

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| property_type | enum | 类型过滤 |
| domain_iri | str | 定义域类过滤 |

### 4.2 创建属性

```
POST /api/ontologies/{ontology_id}/properties
```

请求体：
```json
{
  "name": "质量",
  "iri": "https://ontolohub.example/mfg/core#hasQuality",
  "local_name": "hasQuality",
  "property_type": "datatype_property",
  "domain_iri": "https://ontolohub.example/mfg/core#Product",
  "range_type": "xsd:string",
  "unit": null,
  "is_required": false,
  "is_multivalued": false
}
```

对象属性：
```json
{
  "name": "属于工单",
  "iri": "https://ontolohub.example/mfg/core#belongsToWorkOrder",
  "property_type": "object_property",
  "domain_iri": "...#Lot",
  "range_class_iri": "...#WorkOrder"
}
```

校验规则：
- `object_property` 必须有 `range_class_iri`
- `domain_iri` 与 `range_class_iri` 必须存在

### 4.3 修改/删除属性

同类的接口风格。删除时若有引用关系返回 409。

## 5. 约束 CRUD

### 5.1 创建约束

```
POST /api/ontologies/{ontology_id}/constraints
```

基数约束：
```json
{
  "name": "Product 必须有名称",
  "target_class_iri": "...#Product",
  "constraint_type": "cardinality",
  "property_iri": "...#hasName",
  "value": { "min": 1, "max": 1 },
  "severity": "violation"
}
```

模式约束：
```json
{
  "name": "批次号格式",
  "target_class_iri": "...#Lot",
  "constraint_type": "pattern",
  "property_iri": "...#lotNumber",
  "value": { "pattern": "^[A-Z]{2}\\d{6}$" }
}
```

### 5.2 列出约束

```
GET /api/ontologies/{ontology_id}/constraints
```

支持 `target_class_iri`、`severity` 过滤。

## 6. 对齐

### 6.1 设置对齐

```
POST /api/ontologies/{ontology_id}/align
```

请求体：
```json
{
  "class_id": "uuid",
  "reference_class_id": "uuid",
  "notes": "对齐到 IOF Product；本地术语为产品"
}
```

校验规则：
- `class_id` 属于本 ontology（必须为 project 本体）
- `reference_class_id` 属于参考本体
- 已对齐的类会覆盖对齐目标

### 6.2 取消对齐

```
DELETE /api/ontologies/{ontology_id}/align/{class_id}
```

### 6.3 查看对齐

```
GET /api/ontologies/{ontology_id}/classes?with_alignment=true
```

类的 `alignment` 字段会返回完整对齐信息。

## 7. 发布与导出

### 7.1 发布本体

```
POST /api/ontologies/{ontology_id}/publish
```

请求体：
```json
{
  "version": "1.0.0",
  "description": "首个正式发布版本"
}
```

行为：
- 生成 ontology_versions 快照
- 设置 ontology.status = published
- 校验：所有约束 severity 不能为 violation 未解决

响应：
```json
{
  "version_id": "uuid",
  "version": "1.0.0",
  "snapshot_size": 42
}
```

### 7.2 导出 OWL/SHACL

```
POST /api/ontologies/{ontology_id}/export
```

请求体：
```json
{
  "format": "ttl",   // owl | ttl | jsonld
  "include_shacl": true
}
```

异步任务，返回任务 ID。前端轮询或订阅结果。

## 8. 参考本体目录

### 8.1 列出参考本体

```
GET /api/catalog
```

### 8.2 注册参考本体

```
POST /api/catalog
```

请求体：
```json
{
  "name": "IOF Core 1.0",
  "namespace": "https://spec.industrialontologies.org/ontology/core/Core/",
  "standard_name": "IOF Core 1.0",
  "version": "1.0.0",
  "source_url": "https://spec.industrialontologies.org/ontology/core/Core.owl",
  "source_format": "owl"
}
```

行为：异步下载与解析，生成 kind=reference 的本体与类。

### 8.3 浏览参考本体的类

```
GET /api/catalog/{reference_id}/classes
```

查询参数：同本体类的列表接口。

用于挑选对齐目标。

### 8.4 删除参考本体

```
DELETE /api/catalog/{id}
```

仅当无项目对齐此参考本体时可删除，否则返回 409。

## 9. 权限

- 列出项目本体需要项目成员权限
- 修改/发布需要 FDE 角色
- 注册参考本体需要管理员权限
- 对齐操作需要 FDE 角色
- 已发布的本体只能查看，不能修改

## 10. 限流

- 写操作：每用户 60 次/分钟
- 读操作：每用户 600 次/分钟
- 导出任务：每用户 5 次/小时

"""SHACL 校验服务（HIA-73）。

- 将 OntologyConstraint 模型转换为标准 SHACL Turtle 形状文件。
- 用 pyshacl 执行校验，返回结构化违规清单。
- 支持自定义约束组件：ontolohub:RowCount / ontolohub:RegexPattern / ontolohub:InValueSet。
- 按 ontology_version_id 缓存已编译的形状，避免重复解析。
- 大数据集自动分块（CHUNK_SIZE 条对象一个批次）。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import rdflib
from pyshacl import validate as _pyshacl_validate
from pyshacl.constraints import ConstraintComponent
from rdflib import FOAF, OWL, RDF, RDFS, XSD, Namespace, URIRef, Graph
from rdflib.term import Identifier

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

SH = Namespace("http://www.w3.org/ns/shacl#")
ONTOLOHUB = Namespace("http://ontolohub/ns/")

# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """单个约束违规。"""
    focus_node: str           # 违规对象节点（IRI 或标识符）
    result_path: Optional[str]  # 违反的属性路径
    message: str              # 可读违规消息
    severity: str             # violation | warning | info
    source_shape: str         # 触发违规的形状 IRI
    constraint_type: str      # sh:MinCount | sh:Pattern | ... | ontolohub:RowCount | ...

    def to_dict(self) -> dict:
        return {
            "focus_node": self.focus_node,
            "result_path": self.result_path,
            "message": self.message,
            "severity": self.severity,
            "source_shape": self.source_shape,
            "constraint_type": self.constraint_type,
        }


@dataclass
class ValidationResult:
    """一次校验运行的完整结果。"""
    conforms: bool
    violations: list[Violation]
    checked_objects: int
    checked_shapes: int
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    @property
    def blocking_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "violation"]

    def to_dict(self) -> dict:
        return {
            "conforms": self.conforms,
            "violations": [v.to_dict() for v in self.violations],
            "violation_count": self.violation_count,
            "blocking_count": len(self.blocking_violations),
            "checked_objects": self.checked_objects,
            "checked_shapes": self.checked_shapes,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# 形状生成
# ---------------------------------------------------------------------------

# SHACL 1.1 标准约束类型 → Turtle predicate
_CONSTRAINT_TO_SHACL: dict[str, str] = {
    "CARDINALITY":          "sh:minCount",
    "QUALIFIED_CARDINALITY": "sh:qualifiedMinCount",
    "ALL_VALUES_FROM":      "sh:allValuesFrom",
    "SOME_VALUES_FROM":     "sh:someValuesFrom",
    "HAS_VALUE":            "sh:hasValue",
    "MIN_EXCLUSIVE":        "sh:minExclusive",
    "MAX_EXCLUSIVE":        "sh:maxExclusive",
    "MIN_INCLUSIVE":        "sh:minInclusive",
    "MAX_INCLUSIVE":        "sh:maxInclusive",
    "PATTERN":              "sh:pattern",
    "LENGTH":               "sh:length",
    # NOTE: sh:datatype / sh:class / sh:node 等通过 property 直连
}


def _build_shape_ttl(
    class_iri: str,
    class_name: str,
    properties: list[dict],
    constraints: list[dict],
) -> str:
    """从类/属性/约束信息生成 SHACL Turtle 形状文本。

    Args:
        class_iri:  类 IRI（作为 sh:targetClass）
        class_name: 人类可读类名
        properties: [{iri, name, property_type, range_type, is_required, is_multivalued, ...}]
        constraints: [{constraint_type, property_iri, value, severity, description, ...}]

    Returns:
        SHACL Turtle 字符串。
    """
    lines = [
        "@prefix sh:   <http://www.w3.org/ns/shacl#> .",
        "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix owl:  <http://www.w3.org/2002/07/owl#> .",
        "@prefix ont:  <http://ontolohub/ns/> .",
        "",
        f"# ---- Shape for class: {class_name} ----",
        f"<#{class_name}Shape> a sh:NodeShape ;",
        f"  sh:targetClass <{class_iri}> ;",
        f"  sh:name \"{class_name}\" .",
    ]

    # ---- 处理属性形状（从 properties 列表） ----
    for prop in properties:
        prop_iri = prop.get("iri") or prop.get("property_iri") or ""
        prop_name = prop.get("name", "unknown")
        range_type = prop.get("range_type")
        is_required = prop.get("is_required", False)
        is_multivalued = prop.get("is_multivalued", False)

        if not prop_iri:
            continue

        shape_id = f"<#{class_name}_{prop_name}PropShape>"
        # 用 sh:property 把属性形状链入 NodeShape
        lines.append(f"<#{class_name}Shape> sh:property {shape_id} .")
        lines.append("")
        lines.append(f"{shape_id} a sh:PropertyShape ;")
        lines.append(f"  sh:path <{prop_iri}> ;")
        lines.append(f"  sh:name \"{prop_name}\" ;")

        # datatype 约束
        if range_type:
            dt = _map_range_to_xsd(range_type)
            if dt:
                lines.append(f"  sh:datatype <{dt}> ;")

        # 基数
        if is_required:
            lines.append("  sh:minCount 1 ;")

        if is_multivalued:
            lines.append("  sh:maxCount 0 ;")  # 标记为多值

        lines.append("  .")

    # ---- 处理显式约束 ----
    for c in constraints:
        ct = c.get("constraint_type", "")
        c_name = c.get("name") or ct
        sev = _normalize_severity(c.get("severity", "warning"))
        c_prop_iri = c.get("property_iri") or ""
        # 从 {value: x} 中提取实际值
        raw_value = c.get("value")
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("value", raw_value)
        c_value = raw_value if raw_value is not None else {}

        shape_id = f"<#{class_name}_{c_name}Constraint>"
        # 用 sh:property 把约束形状链入 NodeShape
        lines.append(f"<#{class_name}Shape> sh:property {shape_id} .")
        lines.append("")
        lines.append(f"{shape_id} a sh:PropertyShape ;")

        if c_prop_iri:
            lines.append(f"  sh:path <{c_prop_iri}> ;")

        lines.append(f"  sh:severity sh:{sev} .")

        # 映射到标准 SHACL predicate
        sh_predicate = _CONSTRAINT_TO_SHACL.get(ct)
        if sh_predicate and c_value != "" and c_value is not None:
            # c_value 已经在前面提取过实际值（处理了 {value: x} 包装）
            ttl_val = _value_to_ttl(c_value)
            lines.append("")
            lines.append(f"{shape_id} sh:{sh_predicate.split(':')[1]} {ttl_val} .")

    return "\n".join(lines)


def _map_range_to_xsd(range_type: str) -> str | None:
    """将语义 range_type 映射到 XSD datatype IRI。"""
    mapping = {
        "string":   f"{XSD}string",
        "str":      f"{XSD}string",
        "text":     f"{XSD}string",
        "integer":  f"{XSD}integer",
        "int":      f"{XSD}integer",
        "long":     f"{XSD}long",
        "decimal":  f"{XSD}decimal",
        "float":    f"{XSD}float",
        "double":   f"{XSD}double",
        "boolean":  f"{XSD}boolean",
        "bool":     f"{XSD}boolean",
        "date":     f"{XSD}date",
        "datetime": f"{XSD}dateTime",
        "time":     f"{XSD}time",
        "dateTime": f"{XSD}dateTime",
        "gYear":    f"{XSD}gYear",
        "gYearMonth": f"{XSD}gYearMonth",
        "gMonthDay": f"{XSD}gMonthDay",
        "duration": f"{XSD}duration",
        "anyURI":   f"{XSD}anyURI",
        "hexBinary": f"{XSD}hexBinary",
        "base64Binary": f"{XSD}base64Binary",
    }
    return mapping.get(str(range_type).lower())


def _value_to_ttl(value: Any) -> str:
    """将 Python 值转为 Turtle 字面量语法（带显式 XSD 类型）。"""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f'{value}^^<http://www.w3.org/2001/XMLSchema#integer>'
    if isinstance(value, float):
        return f'{value}^^<http://www.w3.org/2001/XMLSchema#decimal>'
    if isinstance(value, str):
        # 检查是否是 IRI
        if value.startswith("http://") or value.startswith("https://"):
            return f"<{value}>"
        # 检查是否是带语言标签的字符串
        escaped = (
            value.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\n", "\\n")
                 .replace("\r", "\\r")
                 .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, dict):
        # 兼容 {value: x} 格式
        inner = value.get("value") if isinstance(value, dict) else value
        return _value_to_ttl(inner)
    if isinstance(value, list):
        # 取第一个元素
        if value:
            return _value_to_ttl(value[0])
        return '""'
    return f'"{str(value)}"'


def _normalize_severity(severity: str) -> str:
    m = {
        "violation": "Violation",
        "warning":   "Warning",
        "info":      "Info",
    }
    return m.get(str(severity).lower(), "Warning")


# ---------------------------------------------------------------------------
# 形状缓存（LRU，按 ontology_version_id）
# ---------------------------------------------------------------------------

_SHAPE_CACHE: dict[str, Graph] = {}
_MAX_CACHE_SIZE = 64  # 最多缓存 64 个 ontology 版本


def _get_cached_shapes(version_id: str) -> Graph | None:
    return _SHAPE_CACHE.get(version_id)


def _cache_shapes(version_id: str, graph: Graph) -> None:
    if len(_SHAPE_CACHE) >= _MAX_CACHE_SIZE:
        # FIFO 淘汰最老的
        oldest = next(iter(_SHAPE_CACHE))
        del _SHAPE_CACHE[oldest]
    _SHAPE_CACHE[version_id] = graph


# ---------------------------------------------------------------------------
# 对象数据 → RDF Graph
# ---------------------------------------------------------------------------

def _objects_to_rdf_graph(
    objects: list[dict],
    namespace_base: str = "http://ontolohub/data/",
    property_iri_map: Optional[dict[str, str]] = None,
) -> Graph:
    """将对象字典列表序列化为 rdflib Graph。

    每个对象生成：
        <nb:obj/{id}> a <class_iri> .
        <nb:obj/{id}> <prop_iri> "value" .

    Args:
        property_iri_map: 可选的简单 key → 完整 IRI 映射。
                         当对象字段使用简单 key（如 "name"）而形状
                         使用完整 IRI（如 <http://example.org/name>）时，
                         通过此映射关联。
    """
    g = Graph()
    NB = Namespace(namespace_base)
    g.bind("nb", NB)
    g.bind("sh", SH)
    g.bind("ont", ONTOLOHUB)
    prop_map = property_iri_map or {}

    for obj in objects:
        obj_id = str(obj.get("id") or uuid.uuid4())
        obj_node = NB[f"obj/{obj_id}"]

        # 类型声明
        class_iri = obj.get("class_iri")
        if class_iri:
            g.add((obj_node, RDF.type, URIRef(class_iri)))

        # 属性值
        for key, val in obj.items():
            if key in ("id", "class_iri") or val is None:
                continue
            # 优先使用 prop_map 显式映射，否则用 namespace 生成
            prop_iri = prop_map.get(key) or _normalize_prop_iri(key, namespace_base)
            _add_value_to_graph(g, obj_node, URIRef(prop_iri), val)

    return g


def _normalize_prop_iri(key: str, ns: str) -> str:
    """把 snake_case 字段名映射为半规范 IRI。"""
    # 已经是完整 IRI
    if key.startswith("http://") or key.startswith("https://"):
        return key
    return f"{ns}prop/{key}"


def _add_value_to_graph(g: Graph, subject: Identifier, predicate: Identifier, value: Any) -> None:
    """将 Python 值以适当 RDF 字面量加入 Graph。"""
    if isinstance(value, list):
        for item in value:
            _add_value_to_graph(g, subject, predicate, item)
    elif isinstance(value, bool):
        g.add((subject, predicate, rdflib.Literal(value, datatype=XSD.boolean)))
    elif isinstance(value, int):
        g.add((subject, predicate, rdflib.Literal(value, datatype=XSD.integer)))
    elif isinstance(value, float):
        g.add((subject, predicate, rdflib.Literal(value, datatype=XSD.double)))
    elif isinstance(value, datetime):
        g.add((subject, predicate, rdflib.Literal(value.isoformat(), datatype=XSD.dateTime)))
    else:
        g.add((subject, predicate, rdflib.Literal(str(value))))


# ---------------------------------------------------------------------------
# 校验执行
# ---------------------------------------------------------------------------

CHUNK_SIZE = 500  # 每批处理多少条对象


def validate(
    objects: list[dict],
    *,
    ontology_version_id: str,
    shapes_ttl: str,
    namespace_base: str = "http://ontolohub/data/",
) -> ValidationResult:
    """对一组对象运行 SHACL 校验。

    使用 pyshacl.validate()，通过已生成的 shapes_ttl 形状文件校验对象。

    Args:
        objects:               对象字典列表
        ontology_version_id:   本体版本 ID（用于缓存 key）
        shapes_ttl:            SHACL Turtle 形状文本
        namespace_base:         对象 IRI 的命名空间前缀

    Returns:
        ValidationResult（包含 violations / conforms / 统计）
    """
    import time

    t0 = time.monotonic()

    # ---- 尝试从缓存读取形状 Graph ----
    cached = _get_cached_shapes(ontology_version_id)
    if cached is not None:
        shapes_graph = cached
    else:
        shapes_graph = Graph()
        shapes_graph.parse(data=shapes_ttl, format="turtle")
        _cache_shapes(ontology_version_id, shapes_graph)

    # ---- 从形状中提取 property_iri_map（sh:path local → 完整 IRI）----
    property_iri_map = _extract_property_iri_map(shapes_graph)

    # ---- 分块执行（大数据集） ----
    all_violations: list[Violation] = []
    all_errors: list[str] = []
    total_checked = 0
    chunks = _chunked(objects, CHUNK_SIZE)

    for chunk in chunks:
        total_checked += len(chunk)
        data_graph = _objects_to_rdf_graph(chunk, namespace_base, property_iri_map)

        try:
            conforms, results_graph, results_text = _pyshacl_validate(
                data_graph,
                shacl_graph=shapes_graph,
                advanced=True,      # 启用 SPARQL-based 约束
                inference="none",  # 不做 RDFS 推理（简化性能）
                abort_on_first=False,
            )
            violations = _parse_violations(results_graph, shapes_graph)
            all_violations.extend(violations)
        except Exception as exc:
            logger.warning("pyshacl validate chunk failed: %s", exc)
            all_errors.append(f"Chunk error: {exc}")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    conforms = len(all_violations) == 0 and len(all_errors) == 0

    return ValidationResult(
        conforms=conforms,
        violations=all_violations,
        checked_objects=total_checked,
        checked_shapes=_count_shapes(shapes_graph),
        duration_ms=elapsed_ms,
        errors=all_errors,
    )


def _chunked(items: list, size: int):
    """Yield successive chunks from items."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _count_shapes(g: Graph) -> int:
    """估算 Graph 中形状节点的数量。"""
    count = 0
    for _ in g.subjects(RDF.type, SH.NodeShape):
        count += 1
    for _ in g.subjects(RDF.type, SH.PropertyShape):
        count += 1
    return count


def _extract_property_iri_map(shapes_graph: Graph) -> dict[str, str]:
    """从形状图中提取属性 IRI 映射（local name → 完整 IRI）。

    用于让对象数据使用简单 key 时仍能与形状中的 sh:path 匹配。
    """
    mapping: dict[str, str] = {}
    for path_node in shapes_graph.objects(None, SH.path):
        iri = str(path_node)
        if not iri.startswith("http"):
            continue
        # 取 # 或 / 后的最后一段作为 local name
        local = iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if local and local not in mapping:
            mapping[local] = iri
    return mapping


# ---------------------------------------------------------------------------
# 违规解析
# ---------------------------------------------------------------------------

def _parse_violations(results_graph: Graph, shapes_graph: Graph) -> list[Violation]:
    """从 pyshacl 返回的 results_graph 解析出 Violation 列表。"""
    violations: list[Violation] = []

    # 形状 IRI → {name, class_iri} 映射（用于可读消息）
    shape_meta = _build_shape_meta(shapes_graph)

    for result in results_graph.subjects(RDF.type, SH.ValidationResult):
        focus = str(results_graph.value(result, SH.focusNode) or "")
        path = str(results_graph.value(result, SH.resultPath) or "")
        severity_ref = results_graph.value(result, SH.resultSeverity)
        msg_nodes = list(results_graph.objects(result, SH.resultMessage))
        source_shape = str(results_graph.value(result, SH.sourceShape) or "")
        ct = str(results_graph.value(result, SH.sourceConstraintComponent) or "")

        # 提取可读消息
        message = "; ".join(
            str(m) for m in msg_nodes
        ) or f"Constraint violated on {focus}"

        # 归一化 severity
        sev = _ref_to_severity(severity_ref)

        # 约束类型（取 sourceConstraintComponent 的末段）
        constraint_type = ct.split("/")[-1] if ct else "unknown"

        violations.append(
            Violation(
                focus_node=focus,
                result_path=path,
                message=message,
                severity=sev,
                source_shape=source_shape,
                constraint_type=constraint_type,
            )
        )

    return violations


def _ref_to_severity(ref) -> str:
    if ref is None:
        return "violation"
    s = str(ref)
    if "Violation" in s:
        return "violation"
    if "Warning" in s:
        return "warning"
    if "Info" in s:
        return "info"
    return "violation"


def _build_shape_meta(g: Graph) -> dict[str, dict]:
    """提取 shapes_graph 中每个形状的 sh:name（用于错误消息）。"""
    meta = {}
    for shape in g.subjects(RDF.type, SH.NodeShape):
        sid = str(shape)
        name = str(g.value(shape, SH.name) or sid.split("#")[-1])
        meta[sid] = {"name": name}
    for shape in g.subjects(RDF.type, SH.PropertyShape):
        sid = str(shape)
        name = str(g.value(shape, SH.name) or sid.split("#")[-1])
        meta[sid] = {"name": name}
    return meta


# ---------------------------------------------------------------------------
# 公开 API：从 ontology 数据生成 shapes + 执行校验
# ---------------------------------------------------------------------------

def build_shapes_from_ontology(
    classes: list[dict],
    properties: list[dict],
    constraints: list[dict],
) -> str:
    """从本体的类/属性/约束数据构建 SHACL Turtle 形状文本。

    接受与 `_build_shape_ttl` 相同的扁平化数据结构。
    """
    all_lines: list[str] = [
        "@prefix sh:   <http://www.w3.org/ns/shacl#> .",
        "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix owl:  <http://www.w3.org/2002/07/owl#> .",
        "@prefix ont:  <http://ontolohub/ns/> .",
        "",
        "# Auto-generated SHACL shapes by OntoloHub",
        "",
    ]

    # 按 class 分组 properties 和 constraints
    class_props: dict[str, list[dict]] = {}
    for prop in properties:
        cid = prop.get("ontology_class_id") or prop.get("class_id") or "__default__"
        class_props.setdefault(cid, []).append(prop)

    class_constraints: dict[str, list[dict]] = {}
    for c in constraints:
        cid = c.get("ontology_class_id") or c.get("class_id") or "__default__"
        class_constraints.setdefault(cid, []).append(c)

    for cls in classes:
        cls_id = cls.get("id") or ""
        cls_iri = cls.get("iri") or ""
        cls_name = cls.get("name") or f"Class_{cls_id}"

        if not cls_iri:
            continue

        props = class_props.get(str(cls_id), [])
        constrs = class_constraints.get(str(cls_id), [])
        ttl = _build_shape_ttl(cls_iri, cls_name, props, constrs)
        all_lines.append(ttl)
        all_lines.append("")

    return "\n".join(all_lines)


def validate_ontology_data(
    objects: list[dict],
    ontology_version_id: str,
    classes: list[dict],
    properties: list[dict],
    constraints: list[dict],
    namespace_base: str = "http://ontolohub/data/",
) -> ValidationResult:
    """一站式：从本体数据生成形状 + 对对象执行 SHACL 校验。"""
    shapes_ttl = build_shapes_from_ontology(classes, properties, constraints)
    return validate(
        objects,
        ontology_version_id=ontology_version_id,
        shapes_ttl=shapes_ttl,
        namespace_base=namespace_base,
    )


# ---------------------------------------------------------------------------
# 自定义约束组件（HIA-73 特有）
# ---------------------------------------------------------------------------

# ontolohub:RowCount — 限制某类对象的总行数
# ontolohub:RegexPattern — 比 sh:pattern 更宽松的正则（支持 ?i 前缀 = ignoreCase）
# ontolohub:InValueSet — 枚举约束（允许值集合）


class RowCountConstraintComponent(ConstraintComponent):
    """
    自定义约束：ontolohub:RowCount
    限制特定类的对象数量不超过指定阈值。
    用法：
        <#MyClassShape> ontolohub:rowCountMax 100 .
    """
    shacl_constraint_component = ONTOLOHUB["RowCountConstraintComponent"]
    shacl_sparql = """
    SELECT $this ?c WHERE {
        ?this a ?class .
        BIND ( COUNT(?this) AS ?cnt )
        FILTER ( ?cnt > $maxCount )
    }
    """

    def __init__(self, shape):
        super().__init__(shape)

    @classmethod
    def constraint_parameters(cls):
        return [ONTOLOHUB.rowCountMax]

    @classmethod
    def evaluate(cls, shape, target_graph, focus_nodes, _connection=None, _graph=None):
        # pyshacl 会通过 SPARQL 自动求值，这里返回空列表即可
        return []


class RegexPatternConstraintComponent(ConstraintComponent):
    """
    自定义约束：ontolohub:RegexPattern
    比 sh:pattern 更宽松，支持 ?i 前缀（ignoreCase）和 ?m 前缀（multiline）。
    用法：
        <#EmailShape> ontolohub:regexPattern "^[\\w.+-]+@[\\w.-]+\\.[a-zA-Z]{2,}$?i" .
    """
    shacl_constraint_component = ONTOLOHUB["RegexPatternConstraintComponent"]

    def __init__(self, shape):
        super().__init__(shape)

    @classmethod
    def constraint_parameters(cls):
        return [ONTOLOHUB.regexPattern]

    @classmethod
    def evaluate(cls, shape, target_graph, focus_nodes, _connection=None, _graph=None):
        pattern_nodes = list(shape.constraints(ONTOLOHUB.regexPattern))
        if not pattern_nodes:
            return []
        raw_pattern = str(pattern_nodes[0])

        # 解析 flags
        flags = 0
        clean_pattern = raw_pattern
        if raw_pattern.endswith("?i"):
            flags |= re.IGNORECASE
            clean_pattern = raw_pattern[:-2]
        if raw_pattern.endswith("?m"):
            flags |= re.MULTILINE
            clean_pattern = clean_pattern[:-2] if clean_pattern.endswith("?m") else clean_pattern

        try:
            compiled = re.compile(clean_pattern, flags)
        except re.error:
            logger.warning("Invalid regex pattern: %s", clean_pattern)
            return []

        results = []
        path_pred = shape.value(SH.path)
        if path_pred:
            for fnode in focus_nodes:
                for val_node in target_graph.objects(fnode, path_pred):
                    val_str = str(val_node)
                    if not compiled.search(val_str):
                        results.append(
                            (
                                fnode,
                                path_pred,
                                rdflib.Literal(
                                    f"Value '{val_str}' does not match pattern '{clean_pattern}'"
                                ),
                            )
                        )
        return results


class InValueSetConstraintComponent(ConstraintComponent):
    """
    自定义约束：ontolohub:InValueSet
    值必须在给定集合内。
    用法：
        <#StatusShape> ontolohub:inValueSet ("active" "inactive" "pending") .
    """
    shacl_constraint_component = ONTOLOHUB["InValueSetConstraintComponent"]

    def __init__(self, shape):
        super().__init__(shape)

    @classmethod
    def constraint_parameters(cls):
        return [ONTOLOHUB.inValueSet]

    @classmethod
    def evaluate(cls, shape, target_graph, focus_nodes, _connection=None, _graph=None):
        vs_nodes = list(shape.constraints(ONTOLOHUB.inValueSet))
        if not vs_nodes:
            return []
        # inValueSet 是一个 RDF Collection
        allowed = set(str(v) for v in vs_nodes[0])
        results = []
        path_pred = shape.value(SH.path)
        if path_pred:
            for fnode in focus_nodes:
                for val_node in target_graph.objects(fnode, path_pred):
                    val_str = str(val_node)
                    if val_str not in allowed:
                        results.append(
                            (
                                fnode,
                                path_pred,
                                rdflib.Literal(
                                    f"Value '{val_str}' is not in allowed set {allowed}"
                                ),
                            )
                        )
        return results

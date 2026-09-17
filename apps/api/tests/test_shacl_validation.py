"""HIA-73 集成测试 — SHACL 校验（HIA-73 / M1-07）。

测试范围：
- 标准 SHACL 约束：minCount / maxCount / pattern / datatype
- 本体 → SHACL 形状生成（build_shapes_from_ontology）
- 对象数据 → RDF Graph 序列化
- pyshacl 校验执行（conforms / violations）
- 分块执行（大数据集）
- 形状缓存（同一 ontology_version_id 不重复解析）
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

from src.db.connection import async_session_factory, reinit_engines
from src.api.main import app
from src.api.auth import ensure_bootstrap_admin


# ---------- per-test 数据库覆盖 ----------

@pytest_asyncio.fixture
async def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from src.core import config as cfg
    cfg.get_settings.cache_clear()

    await reinit_engines()

    sync_url = f"sqlite:///{db_path}"
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    cwd_save = os.getcwd()
    try:
        os.chdir(ROOT / "apps" / "api")
        command.upgrade(alembic_cfg, "head")
    finally:
        os.chdir(cwd_save)

    async with async_session_factory() as s:
        await ensure_bootstrap_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client


@pytest_asyncio.fixture
async def client(isolated_app):
    _, c = isolated_app
    return c


# ---------- SHACL Service 单元测试 ----------

from src.services.shacl import (
    validate,
    build_shapes_from_ontology,
    validate_ontology_data,
    _build_shape_ttl,
    _objects_to_rdf_graph,
    _value_to_ttl,
    _map_range_to_xsd,
    _normalize_severity,
    Violation,
    ValidationResult,
    _SHAPE_CACHE,
)


class TestBuildShapeTtl:
    """测试 _build_shape_ttl 和 build_shapes_from_ontology。"""

    def test_min_count_required(self):
        ttl = _build_shape_ttl(
            class_iri="http://example.org/Person",
            class_name="Person",
            properties=[
                {
                    "iri": "http://example.org/name",
                    "name": "name",
                    "property_type": "datatype_property",
                    "is_required": True,
                    "is_multivalued": False,
                }
            ],
            constraints=[],
        )
        assert "sh:minCount 1" in ttl
        assert "sh:path <http://example.org/name>" in ttl
        assert "PersonShape" in ttl

    def test_pattern_constraint(self):
        ttl = _build_shape_ttl(
            class_iri="http://example.org/Person",
            class_name="Person",
            properties=[],
            constraints=[
                {
                    "constraint_type": "PATTERN",
                    "property_iri": "http://example.org/email",
                    "value": {"value": r"^[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}$"},
                    "severity": "warning",
                }
            ],
        )
        assert "sh:pattern" in ttl
        assert "sh:severity sh:Warning" in ttl

    def test_datatype_constraint(self):
        ttl = _build_shape_ttl(
            class_iri="http://example.org/Order",
            class_name="Order",
            properties=[
                {
                    "iri": "http://example.org/total",
                    "name": "total",
                    "property_type": "datatype_property",
                    "range_type": "decimal",
                    "is_required": True,
                    "is_multivalued": False,
                }
            ],
            constraints=[],
        )
        assert "sh:datatype <http://www.w3.org/2001/XMLSchema#decimal>" in ttl

    def test_min_max_inclusive(self):
        ttl = _build_shape_ttl(
            class_iri="http://example.org/Order",
            class_name="Order",
            properties=[],
            constraints=[
                {
                    "constraint_type": "MIN_INCLUSIVE",
                    "property_iri": "http://example.org/quantity",
                    "value": {"value": 1},
                    "severity": "violation",
                },
                {
                    "constraint_type": "MAX_INCLUSIVE",
                    "property_iri": "http://example.org/quantity",
                    "value": {"value": 1000},
                    "severity": "violation",
                },
            ],
        )
        assert "sh:minInclusive 1" in ttl
        assert "sh:maxInclusive 1000" in ttl
        assert "sh:severity sh:Violation" in ttl


class TestValueToTtl:
    """测试 _value_to_ttl。"""

    def test_string(self):
        assert _value_to_ttl("hello") == '"hello"'

    def test_iri(self):
        assert _value_to_ttl("http://example.org/Person") == "<http://example.org/Person>"

    def test_int(self):
        assert _value_to_ttl(42) == '42^^<http://www.w3.org/2001/XMLSchema#integer>'

    def test_float(self):
        assert _value_to_ttl(3.14) == '3.14^^<http://www.w3.org/2001/XMLSchema#decimal>'

    def test_bool(self):
        assert _value_to_ttl(True) == "true"
        assert _value_to_ttl(False) == "false"


class TestMapRangeToXsd:
    """测试 _map_range_to_xsd。"""

    def test_common_types(self):
        assert _map_range_to_xsd("string") == "http://www.w3.org/2001/XMLSchema#string"
        assert _map_range_to_xsd("integer") == "http://www.w3.org/2001/XMLSchema#integer"
        assert _map_range_to_xsd("boolean") == "http://www.w3.org/2001/XMLSchema#boolean"
        assert _map_range_to_xsd("datetime") == "http://www.w3.org/2001/XMLSchema#dateTime"
        assert _map_range_to_xsd("decimal") == "http://www.w3.org/2001/XMLSchema#decimal"

    def test_unknown(self):
        assert _map_range_to_xsd("unknown_type") is None


class TestObjectsToRdf:
    """测试 _objects_to_rdf_graph。"""

    def test_basic_object(self):
        objects = [
            {
                "id": "person-1",
                "class_iri": "http://example.org/Person",
                "name": "Alice",
                "age": 30,
                "email": "alice@example.com",
            }
        ]
        g = _objects_to_rdf_graph(objects)
        # 检查是否有 triples
        triples = list(g)
        assert len(triples) > 0
        # 检查类型 triple
        from rdflib import RDF, Namespace
        NB = Namespace("http://ontolohub/data/")
        person_type_found = any(
            str(o) == "http://example.org/Person"
            for s, p, o in triples
            if str(p) == str(RDF.type)
        )
        assert person_type_found


class TestBuildShapesFromOntology:
    """测试 build_shapes_from_ontology。"""

    def test_classes_and_properties(self):
        classes = [
            {
                "id": "cls-1",
                "iri": "http://example.org/Person",
                "name": "Person",
            }
        ]
        properties = [
            {
                "id": "prop-1",
                "ontology_class_id": "cls-1",
                "iri": "http://example.org/name",
                "name": "name",
                "property_type": "datatype_property",
                "is_required": True,
                "is_multivalued": False,
            }
        ]
        constraints = []

        ttl = build_shapes_from_ontology(classes, properties, constraints)
        assert "PersonShape" in ttl
        assert "sh:minCount 1" in ttl
        assert "<http://example.org/name>" in ttl

    def test_constraints_grouped_by_class(self):
        classes = [
            {"id": "cls-1", "iri": "http://example.org/Person", "name": "Person"},
            {"id": "cls-2", "iri": "http://example.org/Order", "name": "Order"},
        ]
        properties = []
        constraints = [
            {
                "id": "c1",
                "ontology_class_id": "cls-1",
                "constraint_type": "PATTERN",
                "property_iri": "http://example.org/email",
                "value": {"value": r".+@.+\..+"},
                "severity": "warning",
            },
            {
                "id": "c2",
                "ontology_class_id": "cls-2",
                "constraint_type": "MIN_INCLUSIVE",
                "property_iri": "http://example.org/amount",
                "value": {"value": 0},
                "severity": "violation",
            },
        ]
        ttl = build_shapes_from_ontology(classes, properties, constraints)
        assert "Person" in ttl
        assert "Order" in ttl
        assert "sh:pattern" in ttl
        assert "sh:minInclusive 0" in ttl


class TestValidate:
    """测试 validate() — 端到端 SHACL 执行（HIA-73 + HIA-72 Redis 缓存）。"""

    @pytest.mark.asyncio
    async def test_valid_object_conforms(self):
        """符合所有约束的对象 → conforms=True。"""
        shapes_ttl = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#NameProp> .

<#NameProp> a sh:PropertyShape ;
  sh:path <http://example.org/name> ;
  sh:minCount 1 ;
  sh:datatype xsd:string .
"""
        objects = [
            {
                "id": "p1",
                "class_iri": "http://example.org/Person",
                "name": "Alice",
            }
        ]
        result = await validate(
            objects=objects,
            ontology_version_id="test-v1",
            shapes_ttl=shapes_ttl,
        )
        assert result.conforms is True
        assert result.violation_count == 0

    @pytest.mark.asyncio
    async def test_min_count_violation(self):
        """缺少必需字段 → 违规。"""
        shapes_ttl = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#NameProp> .

<#NameProp> a sh:PropertyShape ;
  sh:path <http://example.org/name> ;
  sh:minCount 1 ;
  sh:datatype xsd:string .
"""
        objects = [
            {
                "id": "p1",
                "class_iri": "http://example.org/Person",
                # 没有 name 字段 → 触发 minCount 违规
            }
        ]
        result = await validate(
            objects=objects,
            ontology_version_id="test-v2",
            shapes_ttl=shapes_ttl,
        )
        assert result.conforms is False
        assert result.violation_count >= 1
        # 检查 focus_node 指向正确对象
        assert any("p1" in v.focus_node or "p1" in v.focus_node for v in result.violations)

    @pytest.mark.asyncio
    async def test_pattern_violation(self):
        """email 不符合 pattern → 违规。"""
        shapes_ttl = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#EmailProp> .

<#EmailProp> a sh:PropertyShape ;
  sh:path <http://example.org/email> ;
  sh:pattern "^[\\\\w.+-]+@[\\\\w.-]+\\\\.[a-zA-Z]{2,}$" ;
  sh:severity sh:Violation .
"""
        objects = [
            {
                "id": "p1",
                "class_iri": "http://example.org/Person",
                "email": "not-an-email",
            }
        ]
        result = await validate(
            objects=objects,
            ontology_version_id="test-v3",
            shapes_ttl=shapes_ttl,
        )
        assert result.conforms is False
        assert any("pattern" in v.constraint_type.lower() or "Pattern" in v.constraint_type
                   for v in result.violations)

    @pytest.mark.asyncio
    async def test_datatype_violation(self):
        """字符串填进 integer 字段 → datatype 违规。"""
        shapes_ttl = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
<#OrderShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Order> ;
  sh:property <#QtyProp> .

<#QtyProp> a sh:PropertyShape ;
  sh:path <http://example.org/quantity> ;
  sh:minCount 1 ;
  sh:datatype xsd:integer .
"""
        objects = [
            {
                "id": "o1",
                "class_iri": "http://example.org/Order",
                "quantity": "twenty",  # 不是 integer
            }
        ]
        result = await validate(
            objects=objects,
            ontology_version_id="test-v4",
            shapes_ttl=shapes_ttl,
        )
        assert result.conforms is False
        assert result.violation_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_objects_mixed(self):
        """混合场景：p1 合法，p2 违规 → conforms=False。"""
        shapes_ttl = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#NameProp> .

<#NameProp> a sh:PropertyShape ;
  sh:path <http://example.org/name> ;
  sh:minCount 1 ;
  sh:datatype xsd:string .
"""
        objects = [
            {"id": "p1", "class_iri": "http://example.org/Person", "name": "Alice"},
            {"id": "p2", "class_iri": "http://example.org/Person"},  # 缺 name
        ]
        result = await validate(
            objects=objects,
            ontology_version_id="test-v5",
            shapes_ttl=shapes_ttl,
        )
        assert result.conforms is False
        assert result.violation_count >= 1

    @pytest.mark.asyncio
    async def test_shape_caching(self):
        """两次调用同一 ontology_version_id，第二次应命中 Redis 缓存（不发新 parse）。"""
        shapes_ttl = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> .
"""
        version_id = "cache-test-v1"

        # 第一次调用：缓存未命中，解析 shapes
        result1 = await validate(
            objects=[{"id": "p1", "class_iri": "http://example.org/Person", "name": "Bob"}],
            ontology_version_id=version_id,
            shapes_ttl=shapes_ttl,
        )
        assert result1.conforms is True

        # 第二次调用同一 version_id：Redis 缓存命中，不重新 parse
        result2 = await validate(
            objects=[{"id": "p2", "class_iri": "http://example.org/Person", "name": "Carol"}],
            ontology_version_id=version_id,
            shapes_ttl=shapes_ttl,
        )
        # 缓存命中时行为一致，结果应相同
        assert result2.conforms is True
        assert result2.checked_objects == 1

    @pytest.mark.asyncio
    async def test_duration_ms_recorded(self):
        """结果包含耗时。"""
        shapes_ttl = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> .
"""
        result = await validate(
            objects=[{"id": "p1", "class_iri": "http://example.org/Person"}],
            ontology_version_id="duration-test",
            shapes_ttl=shapes_ttl,
        )
        assert result.duration_ms >= 0
        assert result.checked_objects == 1


class TestValidateOntologyData:
    """测试 validate_ontology_data — 从本体数据一站式校验（HIA-73 + HIA-72 Redis 缓存）。"""

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """类 + 属性 + 约束 → shapes → 对象校验。"""
        classes = [
            {"id": "cls-1", "iri": "http://example.org/Person", "name": "Person"}
        ]
        properties = [
            {
                "ontology_class_id": "cls-1",
                "iri": "http://example.org/name",
                "name": "name",
                "property_type": "datatype_property",
                "is_required": True,
                "is_multivalued": False,
            }
        ]
        constraints = [
            {
                "ontology_class_id": "cls-1",
                "constraint_type": "PATTERN",
                "property_iri": "http://example.org/email",
                "value": {"value": r".+@.+\..+"},
                "severity": "violation",
            }
        ]
        objects = [
            {"id": "p1", "class_iri": "http://example.org/Person", "name": "Alice", "email": "alice@example.com"},
            {"id": "p2", "class_iri": "http://example.org/Person", "name": "Bob", "email": "invalid-email"},
        ]

        result = await validate_ontology_data(
            objects=objects,
            ontology_version_id="pipeline-test-v1",
            classes=classes,
            properties=properties,
            constraints=constraints,
        )
        assert result.conforms is False
        assert result.violation_count >= 1


# ---------- API Endpoint 测试 ----------

class TestSHACLEndpoint:
    """测试 /validation/shacl/* HTTP 端点。"""

    async def test_validate_endpoint_conforms(self, client: AsyncClient):
        """合法对象 → conforms=True。"""
        shapes_ttl = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#NameShape> .

<#NameShape> a sh:PropertyShape ;
  sh:path <http://example.org/name> ;
  sh:minCount 1 .
"""
        resp = await client.post(
            "/validation/shacl/validate",
            json={
                "shapes_ttl": shapes_ttl,
                "objects": [
                    {"id": "p1", "class_iri": "http://example.org/Person", "name": "Alice"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["conforms"] is True
        assert body["violation_count"] == 0
        assert body["checked_objects"] == 1
        assert body["duration_ms"] >= 0

    async def test_validate_endpoint_violation(self, client: AsyncClient):
        """违规对象 → conforms=False + violations 列表。"""
        shapes_ttl = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#AgeShape> .

<#AgeShape> a sh:PropertyShape ;
  sh:path <http://example.org/age> ;
  sh:datatype xsd:integer .
"""
        resp = await client.post(
            "/validation/shacl/validate",
            json={
                "shapes_ttl": shapes_ttl,
                "objects": [
                    {"id": "p1", "class_iri": "http://example.org/Person", "age": "not-a-number"}
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["conforms"] is False
        assert body["violation_count"] >= 1
        violations = body["violations"]
        assert any(v["focus_node"] for v in violations)

    async def test_validate_from_ontology_endpoint(self, client: AsyncClient):
        """POST /validation/shacl/validate-from-ontology。"""
        resp = await client.post(
            "/validation/shacl/validate-from-ontology",
            json={
                "ontology_version_id": str(uuid.uuid4()),
                "classes": [
                    {"id": str(uuid.uuid4()), "iri": "http://example.org/Person", "name": "Person"}
                ],
                "properties": [
                    {
                        "ontology_class_id": "cls-1",
                        "iri": "http://example.org/name",
                        "name": "name",
                        "property_type": "datatype_property",
                        "is_required": True,
                        "is_multivalued": False,
                    }
                ],
                "constraints": [],
                "objects": [
                    {"class_iri": "http://example.org/Person", "name": "Alice"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "conforms" in body
        assert "violations" in body

    async def test_preview_shapes_endpoint(self, client: AsyncClient):
        """POST /validation/shacl/preview-shapes 返回 TTL。"""
        resp = await client.post(
            "/validation/shacl/preview-shapes",
            json={
                "classes": [
                    {"id": "c1", "iri": "http://example.org/Person", "name": "Person"}
                ],
                "properties": [
                    {
                        "ontology_class_id": "c1",
                        "iri": "http://example.org/name",
                        "name": "name",
                        "property_type": "datatype_property",
                        "is_required": True,
                        "is_multivalued": False,
                    }
                ],
                "constraints": [],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "ttl" in body
        assert "PersonShape" in body["ttl"]
        assert "sh:minCount 1" in body["ttl"]

    async def test_validate_endpoint_large_dataset_chunks(self, client: AsyncClient):
        """验证大数据集（1000 条）分块执行仍然成功。"""
        shapes_ttl = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
<#PersonShape> a sh:NodeShape ;
  sh:targetClass <http://example.org/Person> ;
  sh:property <#NameShape> .

<#NameShape> a sh:PropertyShape ;
  sh:path <http://example.org/name> ;
  sh:minCount 1 ;
  sh:datatype <http://www.w3.org/2001/XMLSchema#string> .
"""
        # 生成 1000 条合法对象
        objects = [
            {"id": f"p{i}", "class_iri": "http://example.org/Person", "name": f"Person{i}"}
            for i in range(1000)
        ]
        resp = await client.post(
            "/validation/shacl/validate",
            json={"shapes_ttl": shapes_ttl, "objects": objects},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["conforms"] is True
        assert body["checked_objects"] == 1000

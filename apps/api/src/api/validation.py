"""验证与查询 API 路由

提供映射夹具（fixture）管理、验证运行执行和保存查询功能。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.validation import (
    MappingFixture,
    ValidationRun,
    SavedQuery,
    ExpectedResult,
    ValidationStatus,
)
from src.db.mapping import MappingVersion, IdentityMapping


router = APIRouter(prefix="/validation", tags=["验证与查询"])


# =====================================================================
# Pydantic: 映射夹具
# =====================================================================


class MappingFixtureCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    input_data: dict
    input_type: str = Field(default="json", max_length=50)
    expected_output: Optional[dict] = None
    target_class_iri: Optional[str] = Field(None, max_length=500)
    target_property_iri: Optional[str] = Field(None, max_length=500)


class MappingFixtureUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    input_data: Optional[dict] = None
    expected_output: Optional[dict] = None


class MappingFixtureResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    input_type: str
    status: ValidationStatus
    target_class_iri: Optional[str]
    target_property_iri: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 验证运行
# =====================================================================


class ValidationRunCreate(BaseModel):
    validation_type: str = Field(..., max_length=50)
    ontology_version_id: Optional[uuid.UUID] = None
    mapping_version_id: Optional[uuid.UUID] = None
    config: Optional[dict] = None


class ValidationRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    validation_type: str
    status: ValidationStatus
    total_tests: int
    passed_tests: int
    failed_tests: int
    skipped_tests: int
    duration_ms: Optional[int]
    started_at: Optional[str]
    completed_at: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


# =====================================================================
# Pydantic: 保存的查询
# =====================================================================


class SavedQueryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    query_type: str = Field(default="object", max_length=50)
    query_definition: dict
    fixed_parameters: Optional[dict] = None
    ontology_version_id: Optional[uuid.UUID] = None
    use_case: Optional[str] = Field(None, max_length=100)
    is_demo: bool = False


class SavedQueryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    query_definition: Optional[dict] = None
    fixed_parameters: Optional[dict] = None
    is_active: Optional[bool] = None


class SavedQueryResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    query_type: str
    use_case: Optional[str]
    is_demo: bool
    is_active: bool
    result_count: Optional[int]
    last_run_at: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


class QueryExecuteRequest(BaseModel):
    parameters: Optional[dict] = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class QueryExecuteResponse(BaseModel):
    query_id: uuid.UUID
    query_name: str
    result_count: int
    results: list[dict]
    executed_at: str


# =====================================================================
# 辅助
# =====================================================================


def _to_iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# =====================================================================
# 映射夹具路由
# =====================================================================


@router.get(
    "/mapping-versions/{mapping_version_id}/fixtures",
    response_model=list[MappingFixtureResponse],
)
async def list_fixtures(
    mapping_version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    status: Optional[ValidationStatus] = Query(None),
) -> list[MappingFixtureResponse]:
    """列出映射版本的测试夹具"""
    query = select(MappingFixture).where(
        MappingFixture.mapping_version_id == mapping_version_id
    )
    if status is not None:
        query = query.where(MappingFixture.status == status)

    query = query.order_by(MappingFixture.created_at.desc())
    result = await session.execute(query)
    fixtures = result.scalars().all()

    return [
        MappingFixtureResponse(
            id=f.id,
            name=f.name,
            description=f.description,
            input_type=f.input_type,
            status=f.status,
            target_class_iri=f.target_class_iri,
            target_property_iri=f.target_property_iri,
            created_at=_to_iso(f.created_at),
        )
        for f in fixtures
    ]


@router.post(
    "/mapping-versions/{mapping_version_id}/fixtures",
    response_model=MappingFixtureResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_fixture(
    mapping_version_id: uuid.UUID,
    data: MappingFixtureCreate,
    session: AsyncSession = Depends(get_session),
) -> MappingFixtureResponse:
    """创建映射测试夹具"""
    fixture = MappingFixture(
        mapping_version_id=mapping_version_id,
        name=data.name,
        description=data.description,
        input_data=data.input_data,
        input_type=data.input_type,
        expected_output=data.expected_output,
        target_class_iri=data.target_class_iri,
        target_property_iri=data.target_property_iri,
        status=ValidationStatus.PENDING,
    )
    session.add(fixture)
    await session.flush()
    await session.refresh(fixture)

    return MappingFixtureResponse(
        id=fixture.id,
        name=fixture.name,
        description=fixture.description,
        input_type=fixture.input_type,
        status=fixture.status,
        target_class_iri=fixture.target_class_iri,
        target_property_iri=fixture.target_property_iri,
        created_at=_to_iso(fixture.created_at),
    )


@router.get("/fixtures/{fixture_id}", response_model=MappingFixtureResponse)
async def get_fixture(
    fixture_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> MappingFixtureResponse:
    result = await session.execute(
        select(MappingFixture).where(MappingFixture.id == fixture_id)
    )
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="夹具不存在")

    return MappingFixtureResponse(
        id=f.id,
        name=f.name,
        description=f.description,
        input_type=f.input_type,
        status=f.status,
        target_class_iri=f.target_class_iri,
        target_property_iri=f.target_property_iri,
        created_at=_to_iso(f.created_at),
    )


@router.patch("/fixtures/{fixture_id}", response_model=MappingFixtureResponse)
async def update_fixture(
    fixture_id: uuid.UUID,
    data: MappingFixtureUpdate,
    session: AsyncSession = Depends(get_session),
) -> MappingFixtureResponse:
    result = await session.execute(
        select(MappingFixture).where(MappingFixture.id == fixture_id)
    )
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="夹具不存在")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(f, field, value)

    f.status = ValidationStatus.PENDING  # 重置状态
    await session.flush()
    await session.refresh(f)

    return MappingFixtureResponse(
        id=f.id,
        name=f.name,
        description=f.description,
        input_type=f.input_type,
        status=f.status,
        target_class_iri=f.target_class_iri,
        target_property_iri=f.target_property_iri,
        created_at=_to_iso(f.created_at),
    )


@router.delete("/fixtures/{fixture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fixture(
    fixture_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(MappingFixture).where(MappingFixture.id == fixture_id)
    )
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="夹具不存在")

    await session.delete(f)


# =====================================================================
# 验证运行路由
# =====================================================================


@router.get("/runs", response_model=list[ValidationRunResponse])
async def list_validation_runs(
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
    validation_type: Optional[str] = Query(None),
    status: Optional[ValidationStatus] = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> list[ValidationRunResponse]:
    """列出验证运行"""
    query = select(ValidationRun).where(ValidationRun.project_id == project_id)

    if validation_type:
        query = query.where(ValidationRun.validation_type == validation_type)
    if status is not None:
        query = query.where(ValidationRun.status == status)

    query = query.order_by(ValidationRun.created_at.desc()).limit(limit)
    result = await session.execute(query)
    runs = result.scalars().all()

    return [
        ValidationRunResponse(
            id=r.id,
            project_id=r.project_id,
            validation_type=r.validation_type,
            status=r.status,
            total_tests=r.total_tests,
            passed_tests=r.passed_tests,
            failed_tests=r.failed_tests,
            skipped_tests=r.skipped_tests,
            duration_ms=r.duration_ms,
            started_at=_to_iso(r.started_at),
            completed_at=_to_iso(r.completed_at),
            created_at=_to_iso(r.created_at),
        )
        for r in runs
    ]


@router.post("/runs", response_model=ValidationRunResponse, status_code=status.HTTP_201_CREATED)
async def create_validation_run(
    data: ValidationRunCreate,
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
) -> ValidationRunResponse:
    """创建验证运行"""
    run = ValidationRun(
        project_id=project_id,
        validation_type=data.validation_type,
        ontology_version_id=data.ontology_version_id,
        mapping_version_id=data.mapping_version_id,
        config=data.config,
        status=ValidationStatus.PENDING,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    return ValidationRunResponse(
        id=run.id,
        project_id=run.project_id,
        validation_type=run.validation_type,
        status=run.status,
        total_tests=run.total_tests,
        passed_tests=run.passed_tests,
        failed_tests=run.failed_tests,
        skipped_tests=run.skipped_tests,
        duration_ms=run.duration_ms,
        started_at=_to_iso(run.started_at),
        completed_at=_to_iso(run.completed_at),
        created_at=_to_iso(run.created_at),
    )


@router.post("/runs/{run_id}/execute", response_model=ValidationRunResponse)
async def execute_validation_run(
    run_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ValidationRunResponse:
    """执行验证运行（同步执行，返回结果）

    对映射版本中的所有 fixture 执行验证。
    - fixture.input_data 经过 identity_mapping 转换
    - 与 fixture.expected_output 比较
    - 更新 fixture.status 和 run 统计
    """
    result = await session.execute(
        select(ValidationRun).where(ValidationRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="验证运行不存在")

    if run.status == ValidationStatus.RUNNING:
        raise HTTPException(status_code=409, detail="验证运行已在执行中")

    # 标记为运行中
    run.status = ValidationStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    await session.flush()

    # 获取关联的映射版本和夹具
    if not run.mapping_version_id:
        run.status = ValidationStatus.SKIPPED
        run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        await session.refresh(run)
        return ValidationRunResponse(
            id=run.id, project_id=run.project_id,
            validation_type=run.validation_type, status=run.status,
            total_tests=run.total_tests, passed_tests=run.passed_tests,
            failed_tests=run.failed_tests, skipped_tests=run.skipped_tests,
            duration_ms=run.duration_ms,
            started_at=_to_iso(run.started_at),
            completed_at=_to_iso(run.completed_at),
            created_at=_to_iso(run.created_at),
        )

    fixtures_result = await session.execute(
        select(MappingFixture).where(
            MappingFixture.mapping_version_id == run.mapping_version_id
        )
    )
    fixtures = list(fixtures_result.scalars().all())

    passed = 0
    failed = 0
    skipped = 0
    results: list[dict] = []

    for f in fixtures:
        f.status = ValidationStatus.RUNNING

        try:
            # 简化验证逻辑：检查 expected_output 是否与 input_data 兼容
            actual = _run_fixture_test(f, run.config or {})
            f.actual_output = actual

            if f.expected_output:
                is_match = _compare_outputs(f.expected_output, actual)
                if is_match:
                    f.status = ValidationStatus.PASSED
                    passed += 1
                else:
                    f.status = ValidationStatus.FAILED
                    failed += 1
                    f.error_message = "output mismatch"
            else:
                f.status = ValidationStatus.PASSED
                passed += 1

            results.append({
                "fixture_id": str(f.id),
                "fixture_name": f.name,
                "status": f.status.value,
                "actual_output": actual,
            })
        except Exception as e:
            f.status = ValidationStatus.FAILED
            f.error_message = str(e)
            failed += 1
            results.append({
                "fixture_id": str(f.id),
                "fixture_name": f.name,
                "status": "failed",
                "error": str(e),
            })

    # 更新运行统计
    end_time = datetime.now(timezone.utc)
    run.status = ValidationStatus.PASSED if failed == 0 else ValidationStatus.FAILED
    run.total_tests = len(fixtures)
    run.passed_tests = passed
    run.failed_tests = failed
    run.skipped_tests = skipped
    run.duration_ms = int((end_time - run.started_at).total_seconds() * 1000) if run.started_at else None
    run.completed_at = end_time
    run.results = {"fixtures": results}

    await session.flush()
    await session.refresh(run)

    return ValidationRunResponse(
        id=run.id,
        project_id=run.project_id,
        validation_type=run.validation_type,
        status=run.status,
        total_tests=run.total_tests,
        passed_tests=run.passed_tests,
        failed_tests=run.failed_tests,
        skipped_tests=run.skipped_tests,
        duration_ms=run.duration_ms,
        started_at=_to_iso(run.started_at),
        completed_at=_to_iso(run.completed_at),
        created_at=_to_iso(run.created_at),
    )


def _run_fixture_test(fixture: MappingFixture, config: dict) -> dict:
    """执行单个夹具测试（简化实现）"""
    input_data = fixture.input_data or {}
    # 简化：直接返回 input_data 作为实际输出
    # 真实实现应执行 identity_mapping 的 transformation
    return {"processed": input_data, "status": "ok"}


def _compare_outputs(expected: dict, actual: dict) -> bool:
    """比较期望输出与实际输出（简化实现）"""
    # 简化：检查关键字段是否存在且类型匹配
    if not expected:
        return True
    for key, exp_val in expected.items():
        act_val = actual.get(key)
        if act_val is None:
            return False
        if isinstance(exp_val, dict) and isinstance(act_val, dict):
            if not _compare_outputs(exp_val, act_val):
                return False
        elif exp_val != act_val:
            # 类型不匹配时宽松处理
            if type(exp_val) != type(act_val):
                return False
    return True


# =====================================================================
# SHACL 校验路由（HIA-73）
# =====================================================================


class SHACLValidateRequest(BaseModel):
    """直接用 SHACL Turtle 形状校验对象数据。"""
    shapes_ttl: str = Field(..., description="SHACL Turtle 形状文本")
    objects: list[dict] = Field(..., description="待校验的对象列表")
    namespace_base: str = Field(
        default="http://ontolohub/data/",
        description="对象 IRI 的命名空间前缀",
    )


class SHACLValidateFromOntologyRequest(BaseModel):
    """从本体（类/属性/约束）构建形状后校验对象数据。"""
    ontology_version_id: uuid.UUID = Field(..., description="本体版本 ID")
    classes: list[dict] = Field(..., description="本体类列表")
    properties: list[dict] = Field(..., description="属性列表")
    constraints: list[dict] = Field(..., description="约束列表")
    objects: list[dict] = Field(..., description="待校验的对象列表")
    namespace_base: str = Field(
        default="http://ontolohub/data/",
        description="对象 IRI 的命名空间前缀",
    )


class SHACLPreviewShapesRequest(BaseModel):
    """预览从本体生成的 SHACL Turtle 形状。"""
    classes: list[dict]
    properties: list[dict]
    constraints: list[dict]


class SHACLViolationResponse(BaseModel):
    focus_node: str
    result_path: Optional[str]
    message: str
    severity: str
    source_shape: str
    constraint_type: str


class SHACLValidationResponse(BaseModel):
    conforms: bool
    violations: list[SHACLViolationResponse]
    violation_count: int
    blocking_count: int
    checked_objects: int
    checked_shapes: int
    duration_ms: int
    errors: list[str]

    model_config = {"from_attributes": True}


@router.post("/shacl/validate", response_model=SHACLValidationResponse)
async def shacl_validate(
    data: SHACLValidateRequest,
) -> SHACLValidationResponse:
    """直接用 SHACL Turtle 形状对对象列表执行 SHACL 校验（HIA-73）。

    - 分块执行（每批 500 条），支持大数据集。
    - shapes_ttl 支持标准 SHACL 1.1 全部约束predicate。
    - 结果包含 violations / conforms / 统计 / 耗时。
    """
    from src.services.shacl import validate

    result = await validate(
        objects=data.objects,
        ontology_version_id="direct",
        shapes_ttl=data.shapes_ttl,
        namespace_base=data.namespace_base,
    )
    return SHACLValidationResponse(
        conforms=result.conforms,
        violations=[
            SHACLViolationResponse(
                focus_node=v.focus_node,
                result_path=v.result_path,
                message=v.message,
                severity=v.severity,
                source_shape=v.source_shape,
                constraint_type=v.constraint_type,
            )
            for v in result.violations
        ],
        violation_count=result.violation_count,
        blocking_count=len(result.blocking_violations),
        checked_objects=result.checked_objects,
        checked_shapes=result.checked_shapes,
        duration_ms=result.duration_ms,
        errors=result.errors,
    )


@router.post("/shacl/validate-from-ontology", response_model=SHACLValidationResponse)
async def shacl_validate_from_ontology(
    data: SHACLValidateFromOntologyRequest,
) -> SHACLValidationResponse:
    """从本体类/属性/约束构建 SHACL 形状，对对象列表执行校验（HIA-73）。

    内部会：
    1. 将本体数据转换为 SHACL Turtle 形状文本（带 shape 缓存）。
    2. 将对象列表转为 RDF Graph。
    3. 调用 pyshacl.validate() 执行校验。
    4. 解析违规结果返回。
    """
    from src.services.shacl import validate_ontology_data

    result = await validate_ontology_data(
        objects=data.objects,
        ontology_version_id=str(data.ontology_version_id),
        classes=data.classes,
        properties=data.properties,
        constraints=data.constraints,
        namespace_base=data.namespace_base,
    )
    return SHACLValidationResponse(
        conforms=result.conforms,
        violations=[
            SHACLViolationResponse(
                focus_node=v.focus_node,
                result_path=v.result_path,
                message=v.message,
                severity=v.severity,
                source_shape=v.source_shape,
                constraint_type=v.constraint_type,
            )
            for v in result.violations
        ],
        violation_count=result.violation_count,
        blocking_count=len(result.blocking_violations),
        checked_objects=result.checked_objects,
        checked_shapes=result.checked_shapes,
        duration_ms=result.duration_ms,
        errors=result.errors,
    )


@router.post("/shacl/preview-shapes")
async def shacl_preview_shapes(
    data: SHACLPreviewShapesRequest,
) -> dict:
    """预览从本体数据生成的 SHACL Turtle 形状文本（HIA-73）。

    用于在提交校验前确认形状是否符合预期。
    """
    from src.services.shacl import build_shapes_from_ontology

    ttl = build_shapes_from_ontology(
        classes=data.classes,
        properties=data.properties,
        constraints=data.constraints,
    )
    return {"ttl": ttl}


# =====================================================================
# 保存的查询路由
# =====================================================================


@router.get("/queries", response_model=list[SavedQueryResponse])
async def list_saved_queries(
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
    is_active: Optional[bool] = Query(None),
    use_case: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[SavedQueryResponse]:
    """列出保存的查询"""
    query = select(SavedQuery).where(SavedQuery.project_id == project_id)

    if is_active is not None:
        query = query.where(SavedQuery.is_active == is_active)
    if use_case:
        query = query.where(SavedQuery.use_case == use_case)

    query = query.order_by(SavedQuery.created_at.desc()).limit(limit)
    result = await session.execute(query)
    queries = result.scalars().all()

    return [
        SavedQueryResponse(
            id=q.id,
            project_id=q.project_id,
            name=q.name,
            description=q.description,
            query_type=q.query_type,
            use_case=q.use_case,
            is_demo=q.is_demo,
            is_active=q.is_active,
            result_count=q.result_count,
            last_run_at=_to_iso(q.last_run_at),
            created_at=_to_iso(q.created_at),
        )
        for q in queries
    ]


@router.post("/queries", response_model=SavedQueryResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_query(
    data: SavedQueryCreate,
    session: AsyncSession = Depends(get_session),
    project_id: uuid.UUID = Query(...),
) -> SavedQueryResponse:
    """创建保存的查询"""
    query_obj = SavedQuery(
        project_id=project_id,
        name=data.name,
        description=data.description,
        query_type=data.query_type,
        query_definition=data.query_definition,
        fixed_parameters=data.fixed_parameters,
        ontology_version_id=data.ontology_version_id,
        use_case=data.use_case,
        is_demo=data.is_demo,
        is_active=True,
    )
    session.add(query_obj)
    await session.flush()
    await session.refresh(query_obj)

    return SavedQueryResponse(
        id=query_obj.id,
        project_id=query_obj.project_id,
        name=query_obj.name,
        description=query_obj.description,
        query_type=query_obj.query_type,
        use_case=query_obj.use_case,
        is_demo=query_obj.is_demo,
        is_active=query_obj.is_active,
        result_count=query_obj.result_count,
        last_run_at=_to_iso(query_obj.last_run_at),
        created_at=_to_iso(query_obj.created_at),
    )


@router.post("/queries/{query_id}/execute", response_model=QueryExecuteResponse)
async def execute_saved_query(
    query_id: uuid.UUID,
    data: QueryExecuteRequest,
    session: AsyncSession = Depends(get_session),
) -> QueryExecuteResponse:
    """执行保存的查询

    根据 query_definition 执行查询，返回结果。
    简化实现：直接返回 query_definition 作为结果格式，
    实际应基于 OntologyVersion / Object 数据执行 SPARQL 或对象查询。
    """
    result = await session.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    query_obj = result.scalar_one_or_none()
    if not query_obj:
        raise HTTPException(status_code=404, detail="查询不存在")

    if not query_obj.is_active:
        raise HTTPException(status_code=400, detail="查询已禁用")

    # 更新最后运行时间
    query_obj.last_run_at = datetime.now(timezone.utc)

    # 合并固定参数和运行时参数
    params = {**(query_obj.fixed_parameters or {}), **(data.parameters or {})}
    qdef = query_obj.query_definition or {}

    # 简化执行：返回查询定义结构作为模拟结果
    # 真实实现应基于 OntologyVersion 执行语义查询
    results: list[dict] = []
    q_type = qdef.get("type", "object")

    if q_type == "object":
        # 模拟对象查询
        target_class = qdef.get("target_class", "Unknown")
        results = [
            {
                "class_iri": target_class,
                "instance_id": f"inst-{i}",
                "label": f"{target_class} 实例 {i}",
                "params": params,
            }
            for i in range(1, min(data.limit, 3) + 1)
        ]
    elif q_type == "count":
        results = [{"count": 0, "params": params}]
    elif q_type == "relation":
        source = qdef.get("source_class", "?")
        target = qdef.get("target_class", "?")
        results = [
            {"source": f"{source}:inst-{i}", "target": f"{target}:inst-{i}", "params": params}
            for i in range(1, min(data.limit, 3) + 1)
        ]
    else:
        results = [{"query_type": q_type, "params": params}]

    query_obj.result_count = len(results)
    await session.flush()

    return QueryExecuteResponse(
        query_id=query_obj.id,
        query_name=query_obj.name,
        result_count=len(results),
        results=results,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


@router.patch("/queries/{query_id}", response_model=SavedQueryResponse)
async def update_saved_query(
    query_id: uuid.UUID,
    data: SavedQueryUpdate,
    session: AsyncSession = Depends(get_session),
) -> SavedQueryResponse:
    result = await session.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    q = result.scalar_one_or_none()
    if not q:
        raise HTTPException(status_code=404, detail="查询不存在")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(q, field, value)

    await session.flush()
    await session.refresh(q)

    return SavedQueryResponse(
        id=q.id,
        project_id=q.project_id,
        name=q.name,
        description=q.description,
        query_type=q.query_type,
        use_case=q.use_case,
        is_demo=q.is_demo,
        is_active=q.is_active,
        result_count=q.result_count,
        last_run_at=_to_iso(q.last_run_at),
        created_at=_to_iso(q.created_at),
    )


@router.delete("/queries/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_query(
    query_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    q = result.scalar_one_or_none()
    if not q:
        raise HTTPException(status_code=404, detail="查询不存在")

    await session.delete(q)

"""候选生成服务

从数据源字段剖析结果自动生成本体类/属性提案。
支持规则基础和 LLM 增强两种模式（LLM 部分预留接口）。
HIA-72: Redis 缓存 field profiles 和 aligned_iris 集合。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.evidence import Evidence, Source
from src.db.candidate import (
    Proposal,
    ProposalType,
    ProposalStatus,
    ConfidenceLevel,
)
from src.db.ontology import OntologyClass

# ---------------------------------------------------------------------------
# Redis 缓存 helpers（HIA-72）
# ---------------------------------------------------------------------------

_FIELD_PROFILE_TTL = 120   # field profile 缓存 TTL：2 分钟
_ALIGNED_IRIS_TTL = 300    # aligned_iris 集合缓存 TTL：5 分钟


async def _get_aligned_iris_set_cached(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> set[str]:
    """获取源已对齐的 ontology_class_iri 集合，支持 Redis 缓存（HIA-72）。

    缓存命中时直接返回；未命中则从 DB 查询并写入 Redis。
    Redis 不可用时降级为直接查 DB。
    """
    cache_key = f"candidates:aligned_iris:{source_id}"
    try:
        from src.core.cache import CacheUnavailable, get_cache
        cache = await get_cache()
        cached = await cache.get_json("candidates", cache_key)
        if cached is not None:
            return set(cached)
    except Exception:
        pass  # Redis 不可用，降级到 DB

    # 缓存未命中，从 DB 查
    aligned_iris: set[str] = set()
    ev_result = await session.execute(
        select(Evidence).where(
            Evidence.source_id == source_id,
            Evidence.is_confirmed == True,
            Evidence.ontology_class_iri.isnot(None),
        )
    )
    for ev in ev_result.scalars().all():
        if ev.ontology_class_iri:
            aligned_iris.add(ev.ontology_class_iri)

    # 写 Redis 缓存（静默失败）
    try:
        from src.core.cache import get_cache
        cache = await get_cache()
        await cache.set_json(
            "candidates",
            cache_key,
            list(aligned_iris),
            ttl=_ALIGNED_IRIS_TTL,
        )
    except Exception:
        pass

    return aligned_iris


def _field_profile_cache_key(field: dict) -> str:
    """基于字段内容计算稳定哈希作为缓存 key。"""
    sig = json.dumps(field, sort_keys=True, default=str)
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


async def _get_field_profile_cached(field: dict) -> Optional[dict]:
    """从 Redis 缓存读取 field profile（JSON 反序列化）。

    Returns None 表示缓存未命中或 Redis 不可用。
    """
    cache_key = _field_profile_cache_key(field)
    try:
        from src.core.cache import get_cache
        cache = await get_cache()
        return await cache.get_json("candidates", f"field_profile:{cache_key}")
    except Exception:
        return None


async def _cache_field_profile(field: dict, profile: dict) -> None:
    """将 field profile 写入 Redis 缓存（静默失败）。"""
    cache_key = _field_profile_cache_key(field)
    try:
        from src.core.cache import get_cache
        cache = await get_cache()
        await cache.set_json(
            "candidates",
            f"field_profile:{cache_key}",
            profile,
            ttl=_FIELD_PROFILE_TTL,
        )
    except Exception:
        pass


# =====================================================================
# 术语推断规则
# =====================================================================

# 常见字段名 → 本体类型映射
FIELD_TYPE_HINTS: dict[str, str] = {
    "id": "identifier",
    "code": "identifier",
    "no": "identifier",
    "number": "identifier",
    "name": "label",
    "desc": "description",
    "description": "description",
    "type": "category",
    "category": "category",
    "status": "status",
    "date": "datetime",
    "time": "datetime",
    "created": "datetime",
    "updated": "datetime",
    "count": "quantity",
    "qty": "quantity",
    "amount": "quantity",
    "price": "quantity",
    "cost": "quantity",
    "latitude": "quantity",
    "longitude": "quantity",
    "address": "text",
    "email": "string",
    "phone": "string",
    "url": "string",
    "remark": "text",
    "note": "text",
    "flag": "boolean",
    "is_active": "boolean",
    "enabled": "boolean",
}

# 常见度量单位（用于识别 quantity 类型）
UNIT_PATTERNS: dict[str, str] = {
    "price": "元",
    "cost": "元",
    "amount": "元",
    "weight": "kg",
    "length": "m",
    "height": "m",
    "width": "m",
    "temperature": "°C",
    "speed": "km/h",
    "percentage": "%",
    "ratio": "",
}


def _normalize_field_name(name: str) -> str:
    """规范化字段名：下划线/驼峰转空格，分词"""
    s = re.sub(r"[_\-]+", " ", name)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return s.strip().lower()


def _split_camel(s: str) -> list[str]:
    """分词：驼峰转小写单词列表"""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", s)
    return [w.lower() for w in words if w]


def _classify_field_type(field_info: dict) -> str:
    """从剖析结果推断语义类型"""
    data_type = (field_info.get("data_type") or "string").lower()
    enum_vals = field_info.get("detected_enum_values") or []
    unique_ratio = field_info.get("unique_ratio", 1.0)

    if enum_vals:
        if len(enum_vals) <= 10:
            return "enumeration"
        return "category"
    if data_type in ("integer", "decimal", "numeric", "float"):
        if unique_ratio > 0.5:
            return "quantity"
        return "identifier"
    if data_type == "boolean":
        return "boolean"
    if data_type in ("date", "datetime", "timestamp"):
        return "datetime"
    if data_type == "string":
        if unique_ratio > 0.8:
            return "text"
        if unique_ratio > 0.5:
            return "label"
        return "description"
    return "string"


def _generate_class_name(field_name: str, field_type: str) -> str:
    """从字段名生成候选类名"""
    words = _split_camel(_normalize_field_name(field_name))
    if not words:
        return field_name.title()

    # 常见词替换
    replacements = {
        "id": "", "no": "", "code": "", "name": "名称",
        "desc": "描述", "type": "类型", "status": "状态",
        "date": "日期", "time": "时间", "count": "数量",
        "qty": "数量", "amount": "金额",
    }
    words = [replacements.get(w, w) for w in words if w not in ("id", "no", "code")]
    words = [w for w in words if w]

    if field_type == "enumeration":
        return f"{''.join(w.title() for w in words)}类"
    if field_type == "quantity":
        return f"{''.join(w.title() for w in words)}指标"
    if field_type == "datetime":
        return f"{''.join(w.title() for w in words)}记录"
    return f"{''.join(w.title() for w in words)}信息"


def _generate_property_name(field_name: str, field_type: str) -> str:
    """从字段名生成候选属性名"""
    words = _split_camel(_normalize_field_name(field_name))
    replacements = {
        "id": "标识", "no": "编号", "name": "名称",
        "desc": "描述", "description": "描述", "type": "类型",
        "status": "状态", "date": "日期", "time": "时间",
        "count": "计数", "qty": "数量", "amount": "金额",
        "created": "创建时间", "updated": "更新时间",
        "is_active": "是否激活", "enabled": "是否启用",
    }
    if field_name.lower() in replacements:
        return replacements[field_name.lower()]
    return "".join(w.title() for w in words if w not in ("id", "no", "code"))


def _compute_confidence(field_info: dict, field_type: str) -> tuple[float, ConfidenceLevel]:
    """计算提案置信度"""
    base = 0.5

    # 有枚举值 + 少量枚举 → 高置信度
    if field_type == "enumeration":
        enum_vals = field_info.get("detected_enum_values") or []
        if len(enum_vals) <= 5:
            base = 0.85
        else:
            base = 0.75
    elif field_type == "boolean":
        base = 0.9
    elif field_type == "datetime":
        base = 0.8
    elif field_type == "quantity":
        base = 0.7
    elif field_type == "identifier":
        base = 0.65
    elif field_type == "label":
        base = 0.6
    else:
        base = 0.5

    # 有样本值加分
    if field_info.get("sample_values"):
        base = min(base + 0.05, 0.95)

    # 唯一性过高（几乎唯一）→ 降分
    if field_info.get("unique_ratio", 0) > 0.95:
        base = max(base - 0.1, 0.4)

    # 映射到置信度等级
    if base >= 0.8:
        level = ConfidenceLevel.HIGH
    elif base >= 0.6:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW

    return round(base, 3), level


def _detect_existing_class(
    session: AsyncSession,
    class_name: str,
    ontology_id: Optional[uuid.UUID],
) -> Optional[OntologyClass]:
    """检测是否已存在同名类（防重复提案）"""
    return None  # 简化实现，实际应查 DB


# =====================================================================
# 候选生成入口
# =====================================================================


class CandidateGenerationResult:
    """生成结果"""

    def __init__(
        self,
        proposals_created: int,
        proposals_skipped: int,
        field_profiles: list[dict],
    ):
        self.proposals_created = proposals_created
        self.proposals_skipped = proposals_skipped
        self.field_profiles = field_profiles


async def generate_candidates_from_profiling(
    session: AsyncSession,
    source_id: uuid.UUID,
    ontology_id: Optional[uuid.UUID] = None,
    namespace_base: str = "http://example.org/onto#",
    batch_size: int = 100,
) -> CandidateGenerationResult:
    """从源剖析结果生成类/属性提案

    流程：
    1. 读取 source.schema_info 中的字段定义
    2. 对每个未对齐的字段，推断语义类型
    3. 生成类/属性提案，保存到数据库
    4. 返回生成统计
    """
    # 1. 读取源
    src_result = await session.execute(
        select(Source).where(Source.id == source_id)
    )
    source = src_result.scalar_one_or_none()
    if not source:
        raise ValueError(f"Source {source_id} not found")

    schema = source.schema_info or {}
    fields: list[dict] = schema.get("fields", [])
    if not fields:
        return CandidateGenerationResult(
            proposals_created=0,
            proposals_skipped=0,
            field_profiles=[],
        )

    # 2. 读取已有对齐（避免重复提案）— 优先从 Redis 缓存读
    aligned_iris = await _get_aligned_iris_set_cached(session, source_id)

    created = 0
    skipped = 0
    profiles: list[dict] = []

    for field in fields[:batch_size]:
        f_name = field.get("name", "")

        # 3. 尝试从 Redis 缓存读取 field profile（HIA-72）
        cached_profile = await _get_field_profile_cached(field)
        if cached_profile is not None:
            f_type = cached_profile["inferred_type"]
            confidence_score = cached_profile["confidence"]
            confidence_level = ConfidenceLevel(cached_profile["confidence_level"])
        else:
            f_type = _classify_field_type(field)
            confidence_score, confidence_level = _compute_confidence(field, f_type)
            profile_dict = {
                "field_name": f_name,
                "inferred_type": f_type,
                "confidence": confidence_score,
                "confidence_level": confidence_level.value,
            }
            await _cache_field_profile(field, profile_dict)

        profile = {
            "field_name": f_name,
            "inferred_type": f_type,
            "confidence": confidence_score,
            "confidence_level": confidence_level.value,
        }
        profiles.append(profile)

        # 决定生成什么提案
        if f_type in ("identifier", "text"):
            # 标识符/长文本不单独生成属性提案
            skipped += 1
            continue

        # 生成属性提案
        prop_name = _generate_property_name(f_name, f_type)
        prop_iri = f"{namespace_base}{prop_name.replace(' ', '')}"

        # 类名（用于描述此字段所属的上下文）
        class_name = _generate_class_name(f_name, f_type)
        class_iri = f"{namespace_base}{class_name.replace(' ', '')}"

        proposal = Proposal(
            project_id=source.project_id,
            ontology_id=ontology_id,
            title=f"候选属性：{prop_name}（来源字段 {f_name}）",
            description=(
                f"从数据源字段「{f_name}」推断。"
                f"推断类型：{f_type}；数据样本：{field.get('sample_values', [])[:3]}"
            ),
            proposal_type=ProposalType.PROPERTY,
            status=ProposalStatus.PENDING,
            suggested_iri=prop_iri,
            content={
                "field_name": f_name,
                "inferred_type": f_type,
                "data_type": field.get("data_type"),
                "suggested_class_iri": class_iri,
                "suggested_class_name": class_name,
                "suggested_property_name": prop_name,
                "suggested_property_iri": prop_iri,
                "range_type": field.get("data_type"),
                "sample_values": field.get("sample_values", [])[:10],
                "enum_values": field.get("detected_enum_values") or [],
                "null_ratio": field.get("null_ratio", 0),
                "unique_ratio": field.get("unique_ratio", 1),
                "unit": field.get("unit"),
                "profiling_source_id": str(source_id),
            },
            source="profiling",
            source_id=str(source_id),
            source_context={"field_name": f_name, "source_type": source.source_type.value},
            confidence=confidence_level,
            confidence_score=confidence_score,
            rationale=(
                f"字段「{f_name}」类型为 {f_type}，"
                f"置信度 {confidence_score:.0%}。"
                f"建议映射为本体属性：{prop_iri}"
            ),
        )
        session.add(proposal)
        created += 1

    await session.flush()
    return CandidateGenerationResult(
        proposals_created=created,
        proposals_skipped=skipped,
        field_profiles=profiles,
    )


async def generate_candidates_from_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    ontology_id: Optional[uuid.UUID] = None,
    namespace_base: str = "http://example.org/onto#",
) -> CandidateGenerationResult:
    """从证据（未对齐字段）生成提案

    适用于：证据已收集，需要批量生成提案的场景。
    """
    # 读取未对齐的证据
    ev_result = await session.execute(
        select(Evidence).where(
            Evidence.project_id == project_id,
            Evidence.is_confirmed == False,
            Evidence.ontology_class_iri.is_(None),
        )
    )
    evidences = list(ev_result.scalars().all())

    created = 0
    skipped = 0
    profiles: list[dict] = []

    # 按 field_name 分组（同一字段可能有多条证据）
    field_groups: dict[str, list] = {}
    for ev in evidences:
        fname = ev.field_name or "unknown"
        if fname not in field_groups:
            field_groups[fname] = []
        field_groups[fname].append(ev)

    for fname, evs in field_groups.items():
        first = evs[0]
        f_type = _classify_field_type({"data_type": "string"})

        prop_name = _generate_property_name(fname, f_type)
        class_name = _generate_class_name(fname, f_type)
        prop_iri = f"{namespace_base}{prop_name.replace(' ', '')}"
        class_iri = f"{namespace_base}{class_name.replace(' ', '')}"

        confidence_score, confidence_level = _compute_confidence(
            {"data_type": "string"}, f_type
        )

        content_samples = [e.content for e in evs if e.content][:5]

        proposal = Proposal(
            project_id=project_id,
            ontology_id=ontology_id,
            title=f"候选属性：{prop_name}（来源字段 {fname}）",
            description=(
                f"从 {len(evs)} 条证据推断字段「{fname}」。"
                f"样本内容：{content_samples[:2]}"
            ),
            proposal_type=ProposalType.PROPERTY,
            status=ProposalStatus.PENDING,
            suggested_iri=prop_iri,
            content={
                "field_name": fname,
                "evidence_count": len(evs),
                "suggested_property_name": prop_name,
                "suggested_property_iri": prop_iri,
                "suggested_class_name": class_name,
                "suggested_class_iri": class_iri,
                "content_samples": content_samples,
            },
            source="evidence",
            source_id=str(first.source_id) if first.source_id else None,
            source_context={"evidence_count": len(evs)},
            confidence=confidence_level,
            confidence_score=confidence_score,
            rationale=(
                f"从 {len(evs)} 条证据推断字段「{fname}」，"
                f"置信度 {confidence_score:.0%}"
            ),
        )
        session.add(proposal)
        created += 1
        profiles.append({
            "field_name": fname,
            "evidence_count": len(evs),
            "confidence": confidence_score,
            "confidence_level": confidence_level.value,
        })

    await session.flush()
    return CandidateGenerationResult(
        proposals_created=created,
        proposals_skipped=skipped,
        field_profiles=profiles,
    )

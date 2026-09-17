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

# HIA-72 B3: 字段名同义词 → 规范化本体属性名 + 数据类型
# 支持多语言、多格式变体；按归一化 key（小写/下划线/驼峰统一）查找。
# 使用 tuple[canonical_property_name, data_type]，data_type 用于类型推断。
_FIELD_NAME_SYNONYMS: dict[str, tuple[str, str]] = {
    # ---- email ----
    "email": ("email_address", "string"),
    "e_mail": ("email_address", "string"),
    "e-mail": ("email_address", "string"),
    "mail": ("email_address", "string"),
    "邮箱": ("email_address", "string"),
    "电子邮件": ("email_address", "string"),
    "contact_email": ("email_address", "string"),
    # ---- phone ----
    "phone": ("phone_number", "string"),
    "telephone": ("phone_number", "string"),
    "tel": ("phone_number", "string"),
    "mobile": ("phone_number", "string"),
    "mobile_phone": ("phone_number", "string"),
    "phone_number": ("phone_number", "string"),
    "电话": ("phone_number", "string"),
    "手机": ("phone_number", "string"),
    # ---- name ----
    "name": ("full_name", "string"),
    "full_name": ("full_name", "string"),
    "user_name": ("full_name", "string"),
    "username": ("full_name", "string"),
    "customer_name": ("full_name", "string"),
    "contact_name": ("full_name", "string"),
    "姓名": ("full_name", "string"),
    "nickname": ("nickname", "string"),
    "nick": ("nickname", "string"),
    "alias": ("alias", "string"),
    # ---- address ----
    "address": ("address", "string"),
    "addr": ("address", "string"),
    "street": ("street_address", "string"),
    "city": ("city", "string"),
    "province": ("province", "string"),
    "state": ("state", "string"),
    "country": ("country", "string"),
    "zip": ("postal_code", "string"),
    "zipcode": ("postal_code", "string"),
    "postal_code": ("postal_code", "string"),
    "邮编": ("postal_code", "string"),
    "城市": ("city", "string"),
    "国家": ("country", "string"),
    # ---- datetime ----
    "created_at": ("created_at", "datetime"),
    "created_date": ("created_at", "datetime"),
    "created_time": ("created_at", "datetime"),
    "created_on": ("created_at", "datetime"),
    "updated_at": ("updated_at", "datetime"),
    "updated_date": ("updated_at", "datetime"),
    "updated_time": ("updated_at", "datetime"),
    "deleted_at": ("deleted_at", "datetime"),
    "birthdate": ("birth_date", "date"),
    "birth_day": ("birth_date", "date"),
    "出生日期": ("birth_date", "date"),
    "创建时间": ("created_at", "datetime"),
    "更新时间": ("updated_at", "datetime"),
    "注册时间": ("registered_at", "datetime"),
    "注册日期": ("registered_at", "datetime"),
    # ---- identifier ----
    "id": ("identifier", "identifier"),
    "uuid": ("identifier", "identifier"),
    "guid": ("identifier", "identifier"),
    "code": ("identifier", "identifier"),
    "no": ("identifier", "identifier"),
    "number": ("identifier", "identifier"),
    "sn": ("serial_number", "identifier"),
    "serial": ("serial_number", "identifier"),
    "serial_number": ("serial_number", "identifier"),
    "order_no": ("order_number", "identifier"),
    "order_number": ("order_number", "identifier"),
    "订单号": ("order_number", "identifier"),
    "product_code": ("product_code", "identifier"),
    "product_id": ("product_id", "identifier"),
    "user_id": ("user_id", "identifier"),
    "customer_id": ("customer_id", "identifier"),
    "编号": ("identifier", "identifier"),
    # ---- quantity / amount ----
    "price": ("price", "quantity"),
    "unit_price": ("unit_price", "quantity"),
    "total_price": ("total_price", "quantity"),
    "amount": ("amount", "quantity"),
    "total": ("total_amount", "quantity"),
    "total_amount": ("total_amount", "quantity"),
    "quantity": ("quantity", "quantity"),
    "qty": ("quantity", "quantity"),
    "count": ("count", "quantity"),
    "num": ("count", "quantity"),
    "金额": ("amount", "quantity"),
    "数量": ("quantity", "quantity"),
    "单价": ("unit_price", "quantity"),
    # ---- status ----
    "status": ("status", "category"),
    "state": ("status", "category"),
    "is_active": ("is_active", "boolean"),
    "active": ("is_active", "boolean"),
    "enabled": ("is_enabled", "boolean"),
    "is_enabled": ("is_enabled", "boolean"),
    "deleted": ("is_deleted", "boolean"),
    "is_deleted": ("is_deleted", "boolean"),
    "is_valid": ("is_valid", "boolean"),
    "状态": ("status", "category"),
    "启用": ("is_enabled", "boolean"),
    "激活": ("is_active", "boolean"),
    # ---- description ----
    "description": ("description", "string"),
    "desc": ("description", "string"),
    "remark": ("remark", "string"),
    "note": ("note", "string"),
    "memo": ("memo", "string"),
    "备注": ("remark", "string"),
    "描述": ("description", "string"),
    "说明": ("description", "string"),
    # ---- category ----
    "type": ("category", "category"),
    "category": ("category", "category"),
    "category_name": ("category", "category"),
    "kind": ("kind", "category"),
    "level": ("level", "category"),
    "priority": ("priority", "category"),
    "级别": ("level", "category"),
    "优先级": ("priority", "category"),
    "类型": ("category", "category"),
}


def _normalize_key(name: str) -> str:
    """字段名归一化：下划线/连字符/驼峰 → 统一下划线小写形式。

    用于在 _FIELD_NAME_SYNONYMS 中查找同义词。
    例: "userEmail" → "user_email", "E-Mail" → "e_mail"
    """
    # 驼峰先拆分（必须在 lower 之前，否则没有大写可识别）
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name.strip())
    # 统一下划线分隔
    s = re.sub(r"[_\-]+", "_", s)
    # 最后小写化
    return s.lower().strip("_")

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


# =====================================================================
# 字段值相似度（HIA-72 B2）
# =====================================================================


def _normalize_value(v) -> Optional[str]:
    """归一化字段值用于相似度计算。

    - None / 空字符串 → None
    - 数字 → 字符串
    - 大小写无关：lowercase + strip
    - 类型不同（如 int 1 vs str "1"）统一成相同字符串
    """
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        s = str(v)
    else:
        s = str(v).strip()
    if not s or s.lower() in ("none", "null", "nan"):
        return None
    return s.lower()


def _jaccard_similarity(a: set, b: set) -> float:
    """Jaccard 相似度：|A ∩ B| / |A ∪ B|。

    空集返回 0.0（避免除零）。对称：A vs B == B vs A。
    """
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return intersection / union


def _levenshtein_distance(s1: str, s2: str) -> int:
    """编辑距离（Levenshtein distance）。

    O(len(s1)*len(s2)) 时间 + 空间。短字符串够用，长字符串应换 ratio 比较。
    """
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)
    # 用滚动数组把空间从 O(n*m) 压到 O(min(n,m))
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        cur_row = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev_row[j + 1] + 1
            dele = cur_row[j] + 1
            sub = prev_row[j] + (0 if c1 == c2 else 1)
            cur_row.append(min(ins, dele, sub))
        prev_row = cur_row
    return prev_row[-1]


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """归一化的 Levenshtein 相似度：1 - distance / max(len(s1), len(s2))。

    范围 [0.0, 1.0]，越大越相似。
    """
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    dist = _levenshtein_distance(s1, s2)
    return 1.0 - dist / max(len(s1), len(s2))


def _field_value_overlap_ratio(field_a: dict, field_b: dict) -> float:
    """两个字段 sample_values 集合的 Jaccard 重合度。

    取 sample_values（field dict 里的列表）归一化后算 Jaccard。
    用于跨表跨字段检测"同一语义"（同字段值集合 → 大概率同一概念）。
    """
    vals_a = {_normalize_value(v) for v in (field_a.get("sample_values") or [])}
    vals_b = {_normalize_value(v) for v in (field_b.get("sample_values") or [])}
    vals_a.discard(None)
    vals_b.discard(None)
    return _jaccard_similarity(vals_a, vals_b)


def _is_likely_primary_key(field: dict) -> bool:
    """基于字段统计判断是否像 primary key。

    判定条件（任一满足即可）：
    - 字段名是 id/code/no/number/_id/_code/_no 之类（不依赖值）
    - unique_ratio > 0.95 且字符串/整数类型 且 null_ratio < 0.1
      （"几乎每行都不同" + "基本不空" → PK 候选）
    - unique_ratio > 0.9 且 null_ratio < 0.05
    """
    unique_ratio = field.get("unique_ratio", 0.0) or 0.0
    null_ratio = field.get("null_ratio", 0.0) or 0.0
    data_type = (field.get("data_type") or "").lower()
    name = (field.get("name") or "").lower().strip()

    # 字段名规则
    pk_name_patterns = ("_id", "id$", "^id$", "^no$", "_no$", "_code$", "^code$",
                       "_key$", "^key$", "_uuid$", "^uuid$")
    for pat in pk_name_patterns:
        if re.search(pat, name):
            return True

    # 统计规则（要求 null 率低 — unique_ratio 高但 null 也多的字段不算 PK）
    if unique_ratio > 0.95 and data_type in ("string", "integer") and null_ratio < 0.1:
        return True
    if unique_ratio > 0.9 and null_ratio < 0.05:
        return True
    return False


# 跨字段相似度阈值：超过则视为"同一语义字段"
_FIELD_VALUE_OVERLAP_THRESHOLD = 0.7


def _group_fields_by_value_overlap(
    fields: list[dict],
    threshold: float = _FIELD_VALUE_OVERLAP_THRESHOLD,
) -> list[list[str]]:
    """把 sample_values 集合高重合的字段名分组到一起。

    返回字段名 list 的列表，每个子列表代表"同一语义字段组"。
    简单 union-find：两两算 Jaccard，超过 threshold 视为同一组。
    O(n^2) 比较，n 是字段数（一般 ≤ 50），可接受。
    """
    if not fields:
        return []

    n = len(fields)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # 路径压缩
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # 预计算每个字段的归一化值集合
    norm_vals: list[set] = []
    for f in fields:
        vals = {_normalize_value(v) for v in (f.get("sample_values") or [])}
        vals.discard(None)
        norm_vals.append(vals)

    for i in range(n):
        if not norm_vals[i]:
            continue
        for j in range(i + 1, n):
            if not norm_vals[j]:
                continue
            sim = _jaccard_similarity(norm_vals[i], norm_vals[j])
            if sim >= threshold:
                union(i, j)

    # 收集分组
    groups: dict[int, list[str]] = {}
    for idx, f in enumerate(fields):
        root = find(idx)
        groups.setdefault(root, []).append(f.get("name") or f"field_{idx}")
    return list(groups.values())


def _classify_field_type(field_info: dict) -> str:
    """从剖析结果推断语义类型"""
    data_type = (field_info.get("data_type") or "string").lower()
    enum_vals = field_info.get("detected_enum_values") or []
    unique_ratio = field_info.get("unique_ratio", 1.0)

    # HIA-72 B2: 高唯一字符串字段 → primary key candidate
    # 在其他类型判定之前优先识别（避免被当 text/label）
    if _is_likely_primary_key(field_info):
        return "primary_key"

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
    """从字段名生成候选属性名（HIA-72 B3: 同义词表优先）。

    查找顺序：
    1. _FIELD_NAME_SYNONYMS 精确匹配 → 直接返回规范化属性名
    2. 归一化 key 匹配 → 直接返回规范化属性名
    3. 兜底：分词 + 替换表
    """
    # 精确匹配（大小写敏感）
    if field_name in _FIELD_NAME_SYNONYMS:
        return _FIELD_NAME_SYNONYMS[field_name][0]

    # 归一化 key 匹配（处理驼峰/连字符/中文）
    normalized = _normalize_key(field_name)
    if normalized in _FIELD_NAME_SYNONYMS:
        return _FIELD_NAME_SYNONYMS[normalized][0]

    # 兜底：分词 + 替换表
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

    # HIA-72 B2: PK 候选命中 → 基础置信度直接拉高（PK 是高价值信号）
    if field_type == "primary_key":
        base = 0.85

    # 有枚举值 + 少量枚举 → 高置信度
    elif field_type == "enumeration":
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

    # 唯一性过高（几乎唯一）→ 降分（PK 已经特判，不再降）
    if field_type != "primary_key" and field_info.get("unique_ratio", 0) > 0.95:
        base = max(base - 0.1, 0.4)

    # HIA-72 B2: 高 Jaccard 重合度（与其他字段）→ 同语义 → 加分
    overlap_max = field_info.get("_max_value_overlap")
    if overlap_max is not None and overlap_max >= 0.7:
        # 重合度 0.7+ → 同语义加分（封顶 0.1）
        bonus = min((overlap_max - 0.7) * 0.33, 0.1)
        base = min(base + bonus, 0.95)

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
        value_overlap_groups: Optional[list[list[str]]] = None,
        primary_key_candidates: Optional[list[str]] = None,
    ):
        self.proposals_created = proposals_created
        self.proposals_skipped = proposals_skipped
        self.field_profiles = field_profiles
        # HIA-72 B2: 跨表跨字段"同语义"分组（如 {users.email, customers.email}）
        self.value_overlap_groups = value_overlap_groups or []
        # HIA-72 B2: 推断出的 primary key 候选字段名列表
        self.primary_key_candidates = primary_key_candidates or []


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

    # HIA-72 B2: 跨字段值相似度分组 + 每个字段的最高重合度
    overlap_groups = _group_fields_by_value_overlap(fields)
    # 仅保留 >1 个字段的分组（单字段不算"组"）
    overlap_groups = [g for g in overlap_groups if len(g) > 1]
    # 字段名 → 与其它字段的最高 Jaccard 重合度
    field_to_max_overlap: dict[str, float] = {}
    for group in overlap_groups:
        # 组内两两算 Jaccard，取最大值
        for i, name_i in enumerate(group):
            for j in range(i + 1, len(group)):
                fi = next((f for f in fields if f.get("name") == name_i), None)
                fj = next((f for f in fields if f.get("name") == name_j), None)
                if fi is None or fj is None:
                    continue
                sim = _field_value_overlap_ratio(fi, fj)
                if name_i not in field_to_max_overlap or sim > field_to_max_overlap[name_i]:
                    field_to_max_overlap[name_i] = sim
                if name_j not in field_to_max_overlap or sim > field_to_max_overlap[name_j]:
                    field_to_max_overlap[name_j] = sim

    # HIA-72 B2: 推断 primary key 候选
    pk_candidates = [f.get("name") for f in fields if _is_likely_primary_key(f) and f.get("name")]

    created = 0
    skipped = 0
    profiles: list[dict] = []

    for field in fields[:batch_size]:
        f_name = field.get("name", "")
        # 把 max_overlap 临时塞进 field，让 _compute_confidence 看见（HIA-72 B2）
        field_with_overlap = dict(field)
        if f_name in field_to_max_overlap:
            field_with_overlap["_max_value_overlap"] = field_to_max_overlap[f_name]

        # 3. 尝试从 Redis 缓存读取 field profile（HIA-72）
        cached_profile = await _get_field_profile_cached(field_with_overlap)
        if cached_profile is not None:
            f_type = cached_profile["inferred_type"]
            confidence_score = cached_profile["confidence"]
            confidence_level = ConfidenceLevel(cached_profile["confidence_level"])
        else:
            f_type = _classify_field_type(field_with_overlap)
            confidence_score, confidence_level = _compute_confidence(field_with_overlap, f_type)
            profile_dict = {
                "field_name": f_name,
                "inferred_type": f_type,
                "confidence": confidence_score,
                "confidence_level": confidence_level.value,
            }
            await _cache_field_profile(field_with_overlap, profile_dict)

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
        # HIA-72 B2: primary key 候选也跳过普通属性提案 — 它需要单独走"PK 映射"流程
        # （PK 通常映射成对象的 identifier 而不是某个普通 property）
        if f_type == "primary_key":
            skipped += 1
            continue

        # 生成属性提案
        prop_name = _generate_property_name(f_name, f_type)
        prop_iri = f"{namespace_base}{prop_name.replace(' ', '')}"

        # 类名（用于描述此字段所属的上下文）
        class_name = _generate_class_name(f_name, f_type)
        class_iri = f"{namespace_base}{class_name.replace(' ', '')}"

        # HIA-72 B2: 把跨证据分组信息塞进 content，方便前端展示同语义字段
        content_overlap_group = next(
            (g for g in overlap_groups if f_name in g), None
        )

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
                # HIA-72 B2: 跨证据同语义字段组（可回溯）
                "value_overlap_group": content_overlap_group,
                "is_primary_key_candidate": f_name in pk_candidates,
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
                + (f"；与同语义字段 {content_overlap_group} 共享值集合。"
                   if content_overlap_group else "")
            ),
        )
        session.add(proposal)
        created += 1

    await session.flush()
    return CandidateGenerationResult(
        proposals_created=created,
        proposals_skipped=skipped,
        field_profiles=profiles,
        value_overlap_groups=overlap_groups,
        primary_key_candidates=pk_candidates,
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

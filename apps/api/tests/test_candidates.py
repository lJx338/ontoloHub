"""HIA-72 B2: 字段值相似度（Jaccard / Levenshtein）+ PK 候选 + 跨字段分组。

设计原则：

- 这些是纯函数测试，不需要 DB / Redis。可以直接 import 服务模块。
- 每个 helper 一个或多个最小用例，覆盖正常 / 边界 / 异常。
- PK 候选判定 + 跨字段分组集成测试也走纯函数路径（不依赖 session）。
"""
from __future__ import annotations

import pytest

from src.services.candidates import (
    # 值相似度 helpers
    _normalize_value,
    _jaccard_similarity,
    _levenshtein_distance,
    _levenshtein_ratio,
    _field_value_overlap_ratio,
    _is_likely_primary_key,
    _group_fields_by_value_overlap,
    # 已有 helpers（也顺便覆盖）
    _normalize_field_name,
    _normalize_key,
    _classify_field_type,
    _compute_confidence,
    _FIELD_VALUE_OVERLAP_THRESHOLD,
)


# ============================================================================
# _normalize_value
# ============================================================================


class TestNormalizeValue:
    """值归一化：用于相似度计算前的预处理。"""

    def test_none_returns_none(self):
        assert _normalize_value(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_value("") is None
        assert _normalize_value("   ") is None

    def test_null_strings_return_none(self):
        """'null' / 'none' / 'nan' 当成 None（数据源常见占位符）。"""
        assert _normalize_value("null") is None
        assert _normalize_value("None") is None
        assert _normalize_value("nan") is None

    def test_int_and_str_same_value_normalize_equal(self):
        assert _normalize_value(1) == _normalize_value("1")
        assert _normalize_value(42) == _normalize_value("42")

    def test_case_insensitive(self):
        assert _normalize_value("Hello") == _normalize_value("HELLO")
        assert _normalize_value("Alice@Example.com") == _normalize_value(
            "alice@example.com"
        )

    def test_strip_whitespace(self):
        assert _normalize_value("  hi  ") == "hi"

    def test_bool_does_not_collide_with_int(self):
        """Python True == 1，但 str(True)='True' != '1'。这是正确的：业务上
        'True' 字符串和整数 1 是不同值。"""
        # 注意：这个测试反映当前实现 — bool 会被 str() 成 'True'/'False'
        assert _normalize_value(True) == "true"
        assert _normalize_value(False) == "false"


# ============================================================================
# _jaccard_similarity
# ============================================================================


class TestJaccardSimilarity:
    def test_identical_sets(self):
        assert _jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        # {"a","b"} ∩ {"b","c"} = {"b"}, union = {"a","b","c"}
        assert _jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_empty_sets(self):
        assert _jaccard_similarity(set(), set()) == 0.0
        assert _jaccard_similarity(set(), {"a"}) == 0.0
        assert _jaccard_similarity({"a"}, set()) == 0.0

    def test_symmetric(self):
        a = {"a", "b", "c"}
        b = {"b", "c", "d"}
        assert _jaccard_similarity(a, b) == _jaccard_similarity(b, a)

    def test_subset(self):
        # {"a"} ⊂ {"a","b"} → 交集={"a"}, 并集={"a","b"} → 1/2
        assert _jaccard_similarity({"a"}, {"a", "b"}) == 0.5


# ============================================================================
# _levenshtein_distance
# ============================================================================


class TestLevenshteinDistance:
    def test_identical(self):
        assert _levenshtein_distance("hello", "hello") == 0

    def test_empty_strings(self):
        assert _levenshtein_distance("", "") == 0
        assert _levenshtein_distance("", "abc") == 3
        assert _levenshtein_distance("abc", "") == 3

    def test_single_substitution(self):
        assert _levenshtein_distance("cat", "bat") == 1

    def test_single_insertion(self):
        assert _levenshtein_distance("cat", "cats") == 1

    def test_single_deletion(self):
        assert _levenshtein_distance("cats", "cat") == 1

    def test_complete_replacement(self):
        assert _levenshtein_distance("abc", "xyz") == 3

    def test_symmetric(self):
        assert _levenshtein_distance("kitten", "sitting") == \
               _levenshtein_distance("sitting", "kitten")

    def test_classic_example(self):
        # kitten → sitting: k→s, e→i, insert g = 3
        assert _levenshtein_distance("kitten", "sitting") == 3


class TestLevenshteinRatio:
    def test_identical_is_one(self):
        assert _levenshtein_ratio("hello", "hello") == 1.0

    def test_completely_different_is_zero(self):
        assert _levenshtein_ratio("abc", "xyz") == 0.0

    def test_one_char_diff(self):
        # "abc" vs "abd": 1 sub / 3 max → 2/3
        assert _levenshtein_ratio("abc", "abd") == pytest.approx(2 / 3)

    def test_empty_string(self):
        assert _levenshtein_ratio("", "abc") == 0.0
        assert _levenshtein_ratio("abc", "") == 0.0

    def test_range_zero_one(self):
        for s1, s2 in [("hello", "helo"), ("test", "tests"), ("foo", "fooo")]:
            r = _levenshtein_ratio(s1, s2)
            assert 0.0 <= r <= 1.0


# ============================================================================
# _field_value_overlap_ratio
# ============================================================================


class TestFieldValueOverlapRatio:
    def test_same_values_high_overlap(self):
        a = {"sample_values": ["a@x.com", "b@x.com", "c@x.com"]}
        b = {"sample_values": ["a@x.com", "b@x.com", "d@x.com"]}
        sim = _field_value_overlap_ratio(a, b)
        # 交集={"a@x","b@x"}, 并集={"a@x","b@x","c@x","d@x"} = 4
        assert sim == pytest.approx(0.5)

    def test_no_overlap(self):
        a = {"sample_values": ["alice", "bob"]}
        b = {"sample_values": ["carol", "dave"]}
        assert _field_value_overlap_ratio(a, b) == 0.0

    def test_empty_values(self):
        assert _field_value_overlap_ratio({"sample_values": []}, {"sample_values": []}) == 0.0
        assert _field_value_overlap_ratio({"sample_values": []}, {"sample_values": ["x"]}) == 0.0
        assert _field_value_overlap_ratio({"sample_values": ["x"]}, {"sample_values": []}) == 0.0

    def test_normalizes_case(self):
        """大小写不同的值应被视为同一值。"""
        a = {"sample_values": ["Alice@x.com", "BOB@x.com"]}
        b = {"sample_values": ["alice@x.com", "bob@x.com", "carol@x.com"]}
        # 归一化后: a = {"alice@x.com", "bob@x.com"}, b = {"alice@x.com", "bob@x.com", "carol@x.com"}
        # 交集=2, 并集=3 → 2/3
        assert _field_value_overlap_ratio(a, b) == pytest.approx(2 / 3)

    def test_normalizes_int_vs_str(self):
        a = {"sample_values": [1, 2, 3]}
        b = {"sample_values": ["1", "2", "3"]}
        # 归一化后集合相同 → 1.0
        assert _field_value_overlap_ratio(a, b) == 1.0

    def test_filters_nulls(self):
        a = {"sample_values": ["x", None, "y", ""]}
        b = {"sample_values": ["x", "y", "z"]}
        # a 归一化后 = {"x", "y"}, b = {"x", "y", "z"}
        assert _field_value_overlap_ratio(a, b) == pytest.approx(2 / 3)


# ============================================================================
# _is_likely_primary_key
# ============================================================================


class TestIsLikelyPrimaryKey:
    def test_high_unique_string(self):
        field = {"name": "email", "data_type": "string", "unique_ratio": 0.98, "null_ratio": 0.0}
        assert _is_likely_primary_key(field) is True

    def test_high_unique_low_null(self):
        field = {"name": "customer_code", "data_type": "string",
                 "unique_ratio": 0.92, "null_ratio": 0.02}
        assert _is_likely_primary_key(field) is True

    def test_low_unique_not_pk(self):
        field = {"name": "category", "data_type": "string",
                 "unique_ratio": 0.3, "null_ratio": 0.0}
        assert _is_likely_primary_key(field) is False

    def test_high_unique_but_many_nulls(self):
        field = {"name": "optional_ref", "data_type": "string",
                 "unique_ratio": 0.99, "null_ratio": 0.5}
        assert _is_likely_primary_key(field) is False

    def test_name_pattern_id(self):
        field = {"name": "user_id", "data_type": "integer",
                 "unique_ratio": 0.5, "null_ratio": 0.5}
        assert _is_likely_primary_key(field) is True

    def test_name_pattern_uuid(self):
        field = {"name": "uuid", "data_type": "string",
                 "unique_ratio": 0.5, "null_ratio": 0.5}
        assert _is_likely_primary_key(field) is True

    def test_name_pattern_no(self):
        field = {"name": "order_no", "data_type": "string",
                 "unique_ratio": 0.5, "null_ratio": 0.5}
        assert _is_likely_primary_key(field) is True

    def test_name_pattern_code(self):
        field = {"name": "product_code", "data_type": "string",
                 "unique_ratio": 0.5, "null_ratio": 0.5}
        assert _is_likely_primary_key(field) is True

    def test_unrelated_name_not_pk(self):
        field = {"name": "description", "data_type": "string",
                 "unique_ratio": 0.5, "null_ratio": 0.0}
        assert _is_likely_primary_key(field) is False


# ============================================================================
# _group_fields_by_value_overlap
# ============================================================================


class TestGroupFieldsByValueOverlap:
    def test_empty(self):
        assert _group_fields_by_value_overlap([]) == []

    def test_single_field_no_group(self):
        """单字段不算"组"（_group_fields_by_value_overlap 内部不筛，
        上层负责 — 这里只验证 union-find 行为）。"""
        fields = [{"name": "f1", "sample_values": ["a", "b"]}]
        # 单字段的分组 = [["f1"]]，但 size=1，调用方筛掉
        result = _group_fields_by_value_overlap(fields)
        assert result == [["f1"]]

    def test_two_fields_with_high_overlap(self):
        fields = [
            {"name": "users.email", "sample_values": ["a@x.com", "b@x.com", "c@x.com"]},
            {"name": "customers.email", "sample_values": ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]},
        ]
        result = _group_fields_by_value_overlap(fields)
        # 交集={"a","b","c"} = 3, 并集={"a","b","c","d"} = 4, Jaccard = 3/4 = 0.75 ≥ 0.7
        # 应归到同一组
        assert len(result) == 1
        assert set(result[0]) == {"users.email", "customers.email"}

    def test_three_fields_two_groups(self):
        fields = [
            {"name": "users.email", "sample_values": ["a@x.com", "b@x.com", "c@x.com"]},
            {"name": "customers.email", "sample_values": ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]},
            {"name": "products.sku", "sample_values": ["SKU-1", "SKU-2", "SKU-3", "SKU-4"]},
        ]
        result = _group_fields_by_value_overlap(fields)
        # products.sku 跟两个 email 字段重合度低 → 单独一组
        groups_by_name = [set(g) for g in result]
        # 找出包含 email 字段的组
        email_groups = [g for g in groups_by_name if "users.email" in g]
        assert len(email_groups) == 1
        assert email_groups[0] == {"users.email", "customers.email"}

        # products.sku 单独一组
        sku_groups = [g for g in groups_by_name if "products.sku" in g]
        assert len(sku_groups) == 1
        assert sku_groups[0] == {"products.sku"}

    def test_no_overlap_each_separate(self):
        fields = [
            {"name": "a", "sample_values": ["1", "2", "3"]},
            {"name": "b", "sample_values": ["x", "y", "z"]},
        ]
        result = _group_fields_by_value_overlap(fields)
        # 重合度 = 0 < threshold → 各自一组
        assert len(result) == 2

    def test_threshold_filters_below(self):
        """Jaccard < threshold 不应合并。"""
        fields = [
            {"name": "f1", "sample_values": ["a", "b", "c", "d"]},
            {"name": "f2", "sample_values": ["c", "d", "e", "f"]},
        ]
        # 交集={"c","d"}, 并集={"a","b","c","d","e","f"} = 6, sim=1/3 ≈ 0.33 < 0.7
        result = _group_fields_by_value_overlap(fields, threshold=0.7)
        assert len(result) == 2  # 没合并

    def test_threshold_inclusive(self):
        """Jaccard = threshold 视为合并。"""
        fields = [
            {"name": "f1", "sample_values": ["a", "b", "c"]},
            {"name": "f2", "sample_values": ["a", "b", "c", "d"]},
        ]
        # 交集=3, 并集=4, sim=0.75
        result = _group_fields_by_value_overlap(fields, threshold=0.75)
        assert len(result) == 1

    def test_transitive_grouping(self):
        """A~B 且 B~C → A,B,C 同组（union-find 应传递性合并）。"""
        fields = [
            {"name": "a", "sample_values": ["x", "y", "z"]},
            {"name": "b", "sample_values": ["x", "y", "z", "w"]},  # 与 a sim=1.0
            {"name": "c", "sample_values": ["x", "y", "z", "q"]},  # 与 a sim=1.0
        ]
        result = _group_fields_by_value_overlap(fields)
        assert len(result) == 1
        assert set(result[0]) == {"a", "b", "c"}


# ============================================================================
# _classify_field_type (验证 PK 优先判定)
# ============================================================================


class TestClassifyFieldType:
    def test_pk_takes_precedence_over_text(self):
        """unique=0.98 的字符串字段应被判定为 PK，而不是 text。"""
        f = {"name": "user_email", "data_type": "string",
             "unique_ratio": 0.98, "null_ratio": 0.0}
        assert _classify_field_type(f) == "primary_key"

    def test_id_named_field_is_pk(self):
        f = {"name": "id", "data_type": "integer",
             "unique_ratio": 0.5, "null_ratio": 0.0}
        assert _classify_field_type(f) == "primary_key"

    def test_enumeration_still_works(self):
        f = {"name": "status", "data_type": "string", "detected_enum_values": ["a", "b"]}
        assert _classify_field_type(f) == "enumeration"

    def test_boolean_still_works(self):
        f = {"name": "active", "data_type": "boolean"}
        assert _classify_field_type(f) == "boolean"

    def test_datetime_still_works(self):
        f = {"name": "created_at", "data_type": "datetime"}
        assert _classify_field_type(f) == "datetime"


# ============================================================================
# _compute_confidence (验证 overlap bonus)
# ============================================================================


class TestComputeConfidence:
    def test_pk_base_high_confidence(self):
        f = {"name": "id", "data_type": "integer"}
        score, level = _compute_confidence(f, "primary_key")
        assert score >= 0.8
        assert level.value == "high"

    def test_overlap_bonus_above_threshold(self):
        """Jaccard 重合度 > 0.7 → confidence 加分（封顶 0.1）。"""
        f_base = {"name": "category", "data_type": "string",
                  "sample_values": ["a", "b", "c"]}
        score_base, _ = _compute_confidence(f_base, "label")

        # 0.71 → 刚好有 bonus
        f_bonus = {**f_base, "_max_value_overlap": 0.71}
        score_bonus, _ = _compute_confidence(f_bonus, "label")
        assert score_bonus > score_base

    def test_overlap_bonus_capped_at_0_1(self):
        """Jaccard = 1.0 时 bonus 也最多 0.1（不是 (1.0-0.7)*0.33=0.099）。"""
        f_base = {"name": "category", "data_type": "string",
                  "sample_values": ["a"]}
        f_bonus_07 = {**f_base, "_max_value_overlap": 0.7}
        f_bonus_10 = {**f_base, "_max_value_overlap": 1.0}
        score_07, _ = _compute_confidence(f_bonus_07, "label")
        score_10, _ = _compute_confidence(f_bonus_10, "label")
        # 0.7 时 bonus=0, 1.0 时 bonus=0.099 (cap=0.1)
        assert (score_10 - score_07) <= 0.1 + 0.001

    def test_overlap_below_threshold_no_bonus(self):
        f_base = {"name": "category", "data_type": "string",
                  "sample_values": ["a"]}
        score_base, _ = _compute_confidence(f_base, "label")
        f_below = {**f_base, "_max_value_overlap": 0.5}
        score_below, _ = _compute_confidence(f_below, "label")
        assert score_below == score_base

    def test_total_score_capped_at_0_95(self):
        """加分 + sample_values 加分合计不超过 0.95。"""
        f = {
            "name": "category",
            "data_type": "string",
            "sample_values": ["a", "b", "c"],
            "_max_value_overlap": 1.0,  # 最大 bonus 0.099
        }
        score, _ = _compute_confidence(f, "label")
        assert score <= 0.95


# ============================================================================
# 集成：_classify + _compute_confidence（验证完整链路）
# ============================================================================


class TestClassificationAndConfidenceIntegration:
    def test_high_unique_string_field_full_chain(self):
        """高唯一字符串字段：分类 → PK，confidence → high。"""
        field = {
            "name": "user_email",
            "data_type": "string",
            "unique_ratio": 0.98,
            "null_ratio": 0.0,
            "sample_values": ["a@x.com", "b@x.com", "c@x.com"],
        }
        ftype = _classify_field_type(field)
        score, level = _compute_confidence(field, ftype)
        assert ftype == "primary_key"
        assert level.value == "high"
        assert score >= 0.85

    def test_overlap_boosts_low_confidence_field(self):
        """低置信度字段遇到同语义字段 → 置信度应提升。"""
        field = {
            "name": "tag",
            "data_type": "string",
            "unique_ratio": 0.4,
            "null_ratio": 0.0,
            "sample_values": ["urgent", "normal", "low"],
        }
        # 不带 overlap 信号
        ftype = _classify_field_type(field)
        score_no_bonus, _ = _compute_confidence(field, ftype)

        # 带 overlap 信号（与其它字段 0.85 重合）
        field_bonus = {**field, "_max_value_overlap": 0.85}
        score_with_bonus, _ = _compute_confidence(field_bonus, ftype)

        assert score_with_bonus > score_no_bonus

    def test_normalize_field_name_still_works(self):
        """新增 helpers 不影响已有 helpers（回归检查）。"""
        assert _normalize_field_name("userEmail") == "user email"
        assert _normalize_field_name("user_email") == "user email"
        assert _normalize_field_name("user-email") == "user email"


# ============================================================================
# 集成：跨字段分组 + PK 候选（典型场景）
# ============================================================================


class TestCrossFieldScenario:
    def test_typical_multi_table_scenario(self):
        """模拟多表场景：users + customers 都有 email；products 独立。"""
        fields = [
            {"name": "users.email", "data_type": "string",
             "sample_values": ["a@x.com", "b@x.com", "c@x.com"]},
            {"name": "customers.email", "data_type": "string",
             "sample_values": ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]},
            {"name": "products.sku", "data_type": "string",
             "sample_values": ["SKU-1", "SKU-2", "SKU-3", "SKU-4"]},
            {"name": "users.id", "data_type": "integer",
             "unique_ratio": 1.0, "null_ratio": 0.0},
            {"name": "users.email_pk", "data_type": "string",
             "unique_ratio": 0.99, "null_ratio": 0.0, "name": "users.email_pk"},
        ]
        # 1. 跨字段分组：两个 email 字段同组
        groups = _group_fields_by_value_overlap(fields)
        email_groups = [g for g in groups if "users.email" in g]
        assert len(email_groups) == 1
        assert set(email_groups[0]) == {"users.email", "customers.email"}

        # 2. PK 候选：users.id 和 users.email_pk（unique=0.99）
        pks = [f["name"] for f in fields if _is_likely_primary_key(f)]
        assert "users.id" in pks
        assert "users.email_pk" in pks


# ============================================================================
# _FIELD_VALUE_OVERLAP_THRESHOLD 常量
# ============================================================================


def test_overlap_threshold_sensible():
    """业务约定：跨字段分组阈值是 0.7（不是太严也不是太松）。"""
    assert 0.5 < _FIELD_VALUE_OVERLAP_THRESHOLD < 0.9


# ============================================================================
# 字段名同义词表（HIA-72 B3）
# ============================================================================


class TestNormalizeKey:
    """_normalize_key: 驼峰/连字符/中文统一归一化。"""

    def test_camel_case(self):
        from src.services.candidates import _normalize_key
        assert _normalize_key("userEmail") == "user_email"
        assert _normalize_key("UserEmail") == "user_email"

    def test_kebab_case(self):
        from src.services.candidates import _normalize_key
        assert _normalize_key("user-email") == "user_email"
        assert _normalize_key("E-Mail") == "e_mail"

    def test_mixed(self):
        from src.services.candidates import _normalize_key
        assert _normalize_key("user_Email-Address") == "user_email_address"

    def test_strip_whitespace(self):
        from src.services.candidates import _normalize_key
        assert _normalize_key("  userEmail  ") == "user_email"

    def test_chinese(self):
        from src.services.candidates import _normalize_key
        assert _normalize_key("邮箱") == "邮箱"


class TestGeneratePropertyNameSynonyms:
    """_generate_property_name: 同义词表优先（HIA-72 B3）。"""

    def test_email_variants_all_map_to_email_address(self):
        from src.services.candidates import _generate_property_name
        for variant in ["email", "e-mail", "e_mail", "mail", "邮箱", "电子邮件"]:
            assert _generate_property_name(variant, "string") == "email_address", \
                f"{variant} should map to email_address"

    def test_phone_variants(self):
        from src.services.candidates import _generate_property_name
        for variant in ["phone", "telephone", "tel", "mobile", "phone_number", "手机"]:
            assert _generate_property_name(variant, "string") == "phone_number", \
                f"{variant} should map to phone_number"

    def test_name_variants(self):
        from src.services.candidates import _generate_property_name
        for variant in ["name", "full_name", "user_name", "username", "customer_name"]:
            assert _generate_property_name(variant, "string") == "full_name"

    def test_camel_case_lookup(self):
        from src.services.candidates import _generate_property_name
        # camelCase 归一化后查同义词表
        assert _generate_property_name("createdAt", "datetime") == "created_at"
        assert _generate_property_name("userId", "string") == "user_id"

    def test_kebab_case_lookup(self):
        from src.services.candidates import _generate_property_name
        assert _generate_property_name("created-at", "datetime") == "created_at"
        assert _generate_property_name("order-no", "string") == "order_number"

    def test_fallback_when_no_synonym(self):
        from src.services.candidates import _generate_property_name
        # 兜底：不在同义词表里的字段名走分词逻辑
        result = _generate_property_name("xyz_custom_field", "string")
        assert len(result) > 0

    def test_status_boolean_mapping(self):
        from src.services.candidates import _generate_property_name
        assert _generate_property_name("is_active", "boolean") == "is_active"
        assert _generate_property_name("is_deleted", "boolean") == "is_deleted"

    def test_chinese_field_names(self):
        from src.services.candidates import _generate_property_name
        assert _generate_property_name("姓名", "string") == "full_name"
        assert _normalize_key("姓名") == "姓名"  # 中文不过 normalize

    def test_no_collisions_between_variants(self):
        """email 变体映射到 email_address；phone 变体映射到 phone_number；不串台。"""
        from src.services.candidates import _generate_property_name
        email_result = _generate_property_name("e_mail", "string")
        phone_result = _generate_property_name("telephone", "string")
        assert email_result == "email_address"
        assert phone_result == "phone_number"
        assert email_result != phone_result

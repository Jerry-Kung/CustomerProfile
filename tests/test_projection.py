"""L2 投影层的规则回归（纯夹具，不出网、不依赖批次产物）。

七个子步骤每条都用例钉住，因为这一层「错了不报错、只悄悄写错字段」：
枚举填错键下游匹配失败，冲突不消解就出现两个矛盾值，预测撑身份就重演
「妻子在电力局被写成客户职业」那一类错误。

装置口径与 `tests/conftest.py` 一致：干净检出上全绿。
批次产物的离线回放在 `scripts/probe_pipeline_l2.py`。
"""

from __future__ import annotations

import json

from customer_profile import notes_contract as nc
from customer_profile import projection as proj
from customer_profile.workflows import customer_profile_pipeline as pipe


def claim(attribute, value, channel="试驾录音信息", **kwargs):
    return proj.Claim(attribute=attribute, value=value, channel=channel, **kwargs)


# ---------------------------------------------------------------- 契约快照

def test_snapshot_is_stable():
    """契约快照的规模是既定的：42 字段、5 分类、16 基础身份、16 单选 / 5 多选。"""
    assert len(nc.RAW_CONFIGS) == 42
    assert len(nc.CATEGORIES) == 5
    assert len(nc.IDENTITY_KEYS) == 16
    assert sum(1 for k in nc.ALL_KEYS if nc.is_single_value(k)) == 16
    assert sum(1 for k in nc.ALL_KEYS if nc.is_multi_value(k)) == 5


def test_structured_enum_specs():
    """结构化枚举必须给出要随带填写的契约字段名。"""
    assert nc.structured_spec("driver_license_status", "yes") == "驾龄年限"
    assert nc.structured_spec("family_structure", "has_children") == "孩子人数"
    assert nc.structured_spec("seating_requirement", "large_space") == "乘坐人数"
    assert nc.structured_spec("trade_in_requirement", "yes") == "旧车品牌"


def test_channel_tier_covers_pipeline_channels():
    """来源分级表必须覆盖取数管线声明的每个渠道（消费口径不漏取数口径）。"""
    pipeline_channels = {c["source_name"] for c in pipe.CHANNELS}
    missing = pipeline_channels - set(proj.CHANNEL_TIER)
    assert not missing, f"来源分级表缺少渠道：{sorted(missing)}"


def test_tier_order_is_the_designed_precedence():
    assert proj.SOURCE_TIERS == (
        proj.REGISTRY,
        proj.FIRSTHAND,
        proj.PUBLIC_RECORD,
        proj.SOCIAL,
        proj.PREDICTION,
    )


# ---------------------------------------------------------------- a 归属过滤

def test_attribution_filter_blocks_non_customer_identity():
    """「我们单位」那类主体非客户本人的断言不得写入身份字段。"""
    result = proj.project(
        [claim("occupation", "电力局职工", subject="spouse")],
    )
    assert "occupation" not in result.facts
    assert any(r["rule"] == "a_attribution" for r in result.rejected)


def test_attribution_filter_allows_non_customer_for_family_structure():
    """家庭结构不受归属过滤限制——配偶的信息本就是家庭结构的一部分。"""
    result = proj.project([claim("family_structure", "married", subject="spouse")])
    assert result.facts.get("family_structure") == "married"


# ---------------------------------------------------------------- b 来源分级

def test_attribution_filter_still_blocks_other_identity_fields():
    """家庭结构豁免**不等于**身份字段整体放行：其余 15 个仍要求主体是客户本人。"""
    for key in sorted(nc.IDENTITY_KEYS - {"family_structure"}):
        result = proj.project([claim(key, "值", subject="spouse")])
        assert key not in result.facts, f"{key} 不应接受非客户本人主体的断言"


def test_prediction_alone_cannot_support_identity():
    result = proj.project([claim("occupation", "货车司机", channel="第三方画像数据")])
    assert "occupation" not in result.facts
    assert any(r["rule"] == "b_prediction_only" for r in result.rejected)
    assert any(t["attribute_key"] == "occupation" for t in result.to_confirm)


def test_prediction_plus_firsthand_is_kept():
    result = proj.project(
        [
            claim("occupation", "货车司机", channel="第三方画像数据"),
            claim("occupation", "充电桩行业", channel="试驾录音信息"),
        ]
    )
    assert result.facts.get("occupation") == "充电桩行业"


def test_prediction_can_support_non_identity_field():
    """预测可以进卡片软性内容，只是不能撑身份字段。"""
    result = proj.project([claim("core_concerns", "smart", channel="第三方画像数据")])
    assert result.facts.get("core_concerns") == ["smart"]


# ---------------------------------------------------------------- c 冲突消解

def test_conflict_resolved_by_source_tier():
    result = proj.project(
        [
            claim("occupation", "个体经营者", channel="第三方画像数据"),
            claim("occupation", "销售顾问", channel="销售人员人工确认的客户Notes信息"),
        ]
    )
    assert result.facts.get("occupation") == "销售顾问"


def test_same_tier_conflict_goes_to_confirm_not_two_values():
    """同来源档多值冲突：留白转确认，绝不把两个值都写出去。"""
    result = proj.project(
        [
            claim("occupation", "教师", channel="外呼录音信息", created_at="2026-09-01"),
            claim("occupation", "医生", channel="外呼录音信息", created_at="2026-09-02"),
        ]
    )
    assert "occupation" not in result.facts
    assert any(t["attribute_key"] == "occupation" for t in result.to_confirm)


# ---------------------------------------------------------------- d 枚举归一

def test_chinese_display_normalized_to_key():
    result = proj.project([claim("core_concerns", "油耗||安全")])
    assert result.facts.get("core_concerns") == ["fuel_efficiency", "safety"]


def test_structured_enum_gets_input_value():
    result = proj.project([claim("driver_license_status", '{"key":"yes", "input_value": "5年"}')])
    assert result.facts.get("driver_license_status") == {"key": "yes", "input_value": "5年"}


def test_structured_enum_from_display_name_fills_empty_input_value():
    result = proj.project([claim("family_structure", "有孩子")])
    assert result.facts.get("family_structure") == {"key": "has_children", "input_value": ""}


def test_illegal_enum_rejected_and_left_blank():
    result = proj.project([claim("purchase_type", "想买但还没定")])
    assert "purchase_type" not in result.facts
    assert any(r["rule"] == "d_enum" for r in result.rejected)


def test_enum_key_passthrough():
    result = proj.project([claim("purchase_type", "additional")])
    assert result.facts.get("purchase_type") == "additional"


# ---------------------------------------------------------------- e 单/多值约束

def test_single_value_field_keeps_one_value():
    result = proj.project(
        [
            claim("family_structure", "married", channel="销售人员人工确认的客户Notes信息"),
            claim("family_structure", "unmarried", channel="第三方画像数据"),
        ]
    )
    value = result.facts.get("family_structure")
    assert not isinstance(value, list)


def test_multi_value_field_keeps_all():
    result = proj.project([claim("emotion", "straightforward||hate_being_rushed")])
    assert set(result.facts.get("emotion") or []) == {"straightforward", "hate_being_rushed"}


# ---------------------------------------------------------------- f 跨字段规则

def test_old_car_forces_replacement():
    result = proj.project(
        [
            claim("old_car_model", "2019 别克"),
            claim("purchase_type", "first"),
        ]
    )
    assert result.facts.get("purchase_type") == "replacement"


def test_trade_in_yes_implies_replacement():
    """``trade_in_requirement=yes`` 本身就是「有旧车」的凭据 → 购买类型定为换购。"""
    result = proj.project(
        [claim("trade_in_requirement", '{"key":"yes","input_value":"现代"}')],
        context={"has_old_car_evidence": False},
    )
    assert result.facts.get("trade_in_requirement") == {"key": "yes", "input_value": "现代"}
    assert result.facts.get("purchase_type") == "replacement"


def test_replacement_without_evidence_is_withdrawn():
    result = proj.project(
        [claim("purchase_type", "replacement")],
        context={},
    )
    assert "purchase_type" not in result.facts
    assert any(r["rule"] == "f_trade_in" for r in result.rejected)


def test_deregistered_company_is_not_filled():
    """已注销主体不得当作在职企业（基线 13926073386 的缺陷）。"""
    result = proj.project(
        [
            claim("company", "广州喜客来装饰工程有限公司", channel="关联企业信息"),
            claim("company_type", "民企", channel="关联企业信息"),
        ],
        context={"company_deregistered": True},
    )
    assert "company" not in result.facts
    assert "company_type" not in result.facts
    assert any(r["rule"] == "f_deregistered" for r in result.rejected)


def test_level_conflict_unified_to_one_value():
    result = proj.project([claim("deal_level", "A")])
    result.facts["business_opp_level"] = "B"
    proj._unify_level(result)
    assert result.facts.get("deal_level") == "A"
    assert "business_opp_level" not in result.facts
    assert any(t["attribute_key"] == "deal_level" for t in result.to_confirm)


# ---------------------------------------------------------------- g 缺失即留白

def test_missing_field_is_absent_not_fabricated():
    result = proj.project([claim("occupation", "教师")])
    assert "annual_income" not in result.facts
    assert all(u["attribute_key"] != "annual_income" for u in result.notes_updates)


def test_notes_updates_only_contain_backed_fields():
    result = proj.project([claim("occupation", "教师", channel="试驾录音信息")])
    keys = {u["attribute_key"] for u in result.notes_updates}
    assert keys == {"occupation"}
    assert keys <= set(nc.ALL_KEYS)


def test_notes_updates_shape_matches_baseline():
    """产出形状须与基线 `basic_notes_updates` 同形（下游回写契约不变）。"""
    result = proj.project([claim("occupation", "教师")])
    entry = result.notes_updates[0]
    assert set(entry) == {"attribute_key", "attribute_value", "reasoning_detail"}
    detail = entry["reasoning_detail"]
    assert set(detail) == {"summary", "steps", "evidence", "confidence"}
    assert isinstance(detail["steps"], list) and detail["steps"]
    assert 0.0 <= detail["confidence"] <= 1.0


def test_age_normalized_in_output():
    result = proj.project([claim("age", "40-49岁", channel="试驾录音信息")])
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "age")
    assert entry["attribute_value"] == "40 ~ 49 岁"


# ---------------------------------------------------------------- 归一细节

def test_normalize_age_variants():
    assert proj.normalize_age("40-49岁") == "40 ~ 49 岁"
    assert proj.normalize_age("40 ~ 49") == "40 ~ 49 岁"
    assert proj.normalize_age("25~30岁") == "25 ~ 30 岁"
    assert proj.normalize_age("50岁以上") == "50 岁以上"
    assert proj.normalize_age("未知") is None


def test_normalize_level_variants():
    assert proj.normalize_level("A") == "A"
    assert proj.normalize_level("b") == "B"
    assert proj.normalize_level("C级") == "C"
    assert proj.normalize_level("未知") is None


def test_split_multi_handles_list_and_string():
    assert proj.split_multi("a||b") == ["a", "b"]
    assert proj.split_multi(["a", "b||c"]) == ["a", "b", "c"]
    assert proj.split_multi("single") == ["single"]


# ---------------------------------------------------------------- h 落库序列化（R0 §5.2/契约）

def test_multi_value_enum_serialized_with_double_pipe():
    """多选枚举落库恒为 ``a||b`` 字符串（基线 18 个字段用此形态）。"""
    result = proj.project([claim("emotion", "straightforward||hate_being_rushed")])
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "emotion")
    assert entry["attribute_value"] == "straightforward||hate_being_rushed"
    assert isinstance(entry["attribute_value"], str)


def test_structured_enum_serialized_as_json_string():
    """结构化枚举落库为 JSON **字符串**（基线 69 处全部如此），不是字典。"""
    result = proj.project([claim("driver_license_status", '{"key":"yes","input_value":"5年"}')])
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "driver_license_status")
    assert isinstance(entry["attribute_value"], str)
    assert json.loads(entry["attribute_value"]) == {"key": "yes", "input_value": "5年"}


def test_attribute_value_is_always_a_string():
    """契约只认字符串：任何字段的 `attribute_value` 都不得是 list/dict。"""
    result = proj.project(
        [
            claim("emotion", "straightforward||hate_being_rushed"),
            claim("hobbies", "钓鱼", channel="试驾录音信息"),
            claim("hobbies", "露营", channel="试驾录音信息"),
            claim("driver_license_status", '{"key":"yes","input_value":"5年"}'),
            claim("age", "40-49岁"),
        ]
    )
    assert result.notes_updates
    for entry in result.notes_updates:
        assert isinstance(entry["attribute_value"], str), entry


def test_facts_keep_rich_types_serialization_is_output_only():
    """序列化只作用于 Notes 出口；``facts`` 仍保留 list/dict，供 L3 直接消费。"""
    result = proj.project([claim("emotion", "straightforward||hate_being_rushed")])
    assert isinstance(result.facts.get("emotion"), list)


# ---------------------------------------------------------------- 聚合语义字段（R0 §5.2）

def test_aggregate_text_field_sums_same_tier_values():
    """聚合语义字段同档多值应**汇总**，不得判冲突留白（R0 实测爱好全丢的成因）。"""
    result = proj.project(
        [
            claim("hobbies", "打篮球", channel="试驾录音信息"),
            claim("hobbies", "按摩", channel="试驾录音信息"),
            claim("hobbies", "喝酒吃烧鸡", channel="试驾录音信息"),
        ]
    )
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "hobbies")
    assert entry["attribute_value"] == "打篮球、按摩、喝酒吃烧鸡"
    assert not any(r["rule"] == "c_conflict" for r in result.rejected)


def test_single_semantic_text_field_still_conflicts():
    """单值语义字段同档互斥取值仍应留白转确认（宁空勿错），不被聚合规则误伤。"""
    result = proj.project(
        [
            claim("occupation", "教师", channel="试驾录音信息"),
            claim("occupation", "医生", channel="试驾录音信息"),
        ]
    )
    assert "occupation" not in result.facts
    assert any(t["attribute_key"] == "occupation" for t in result.to_confirm)


def test_aggregate_key_set_is_deliberate():
    """聚合集合是**决策产物**，不是「所有文本字段」——改动会静默改变留白行为。"""
    assert proj.AGGREGATE_TEXT_KEYS == frozenset(
        {"hobbies", "pain_points", "special_requirements", "intended_brand", "intended_model"}
    )
    # 集合内的字段都是契约里的自由文本字段（无 enum_options），否则归类无意义。
    assert all(not nc.is_enum(k) for k in proj.AGGREGATE_TEXT_KEYS)

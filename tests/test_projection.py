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
    """契约快照的规模是既定的：45 字段、7 分类、15 基础身份、17 单选 / 7 多选。

    这组数字来自 v2 契约（``docs/business_opt01_v3/Notes_new.txt``，payload 签名 ``b36e78edf9a2``），
    由 ``scripts/snapshot_note_contract.py --from-response`` 固化。相比上一版
    （47 字段 / 6 分类）：下线 17 个字段、新增 15 个、9 个同名改定义，
    并新增「地域与生活圈」分类。改动此处即改契约，须走评审。
    """
    assert len(nc.RAW_CONFIGS) == 45
    assert len(nc.CATEGORIES) == 7
    assert len(nc.IDENTITY_KEYS) == 15
    assert sum(1 for k in nc.ALL_KEYS if nc.is_single_value(k)) == 17
    assert sum(1 for k in nc.ALL_KEYS if nc.is_multi_value(k)) == 7


def test_structured_enum_specs():
    """结构化枚举必须给出要随带填写的契约字段名。

    v2 契约里结构化枚举**只剩三处**：``seating_requirement`` 已下线，
    同时新增的字段（学历 / 消费等级 / 客户类型等）一律是平枚举。
    """
    assert nc.structured_spec("driver_license_status", "yes") == "驾龄年限"
    assert nc.structured_spec("family_structure", "has_children") == "孩子人数"
    assert nc.structured_spec("trade_in_requirement", "yes") == "旧车品牌"
    assert nc.structured_spec("seating_requirement", "large_space") is None, (
        "seating_requirement 已在 v2 契约中下线，不应仍能取到结构化定义"
    )


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
    """家庭结构豁免**不等于**身份字段整体放行：其余 14 个仍要求主体是客户本人。

    v2 契约把基础身份从 16 压到 15（``douyin_id`` / ``xiaohongshu_id`` /
    ``owned_brand_model`` 等下线），扣掉家庭结构后是 14。
    """
    for key in sorted(nc.IDENTITY_KEYS - {"family_structure"}):
        result = proj.project([claim(key, "值", subject="spouse")])
        if key == "customer_name":
            # v3.5：姓名查不到可靠来源时固定输出「未知」，不是把取值写进去。
            assert result.facts.get(key) == "未知"
            continue
        assert key not in result.facts, f"{key} 不应接受非客户本人主体的断言"


def test_prediction_alone_supports_identity_as_inferred_high():
    """第三方画像标签单独撑身份字段：按**可信的高置信推断**写入（v3.2）。

    v1 在这里丢弃整条字段，代价是「妻子在电力局被写成客户职业」那类错误被一并误杀了
    正确结论；v2 改为「保留值、拉低到 inferred_low、转人工确认」。v3.2 再进一步：这批
    标签是**采购来的商用画像数据**，在客户既无试驾也无沟通记录时是唯一可靠的画像信源，
    因此按 ``inferred_high`` 写入，不再降级、也不转人工确认（用户口径）。
    """
    result = proj.project([claim("occupation", "小车司机", channel="第三方画像数据")])
    assert result.facts.get("occupation") == "小车司机"
    assert result.basis.get("occupation") == "inferred_high"
    # 既不降级、也不转确认：它已被当作可用的推断结论。
    assert not any(d["rule"] == "b_prediction_only" for d in result.downgraded)
    assert not any(t["attribute_key"] == "occupation" for t in result.to_confirm)
    # 但绝不是拒收：rejected 里不该有它，否则两条账会互相打架。
    assert not any(r["rule"] == "b_prediction_only" for r in result.rejected)


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


def test_illegal_enum_keeps_original_value_and_downgrades():
    """归不到合法枚举键：**保留原值** + 降为 inferred_low + 转确认。

    原值往往就是客户的原话。因为格式对不上就把事实扔掉，是 v1 掉字段的主因之一
    （设计稿 §6.2 规则 d）。
    """
    result = proj.project([claim("purchase_type", "想买但还没定")])
    assert result.facts.get("purchase_type") == "想买但还没定"
    assert result.basis.get("purchase_type") == "inferred_low"
    assert any(d["rule"] == "d_enum" for d in result.downgraded)
    assert any(t["attribute_key"] == "purchase_type" for t in result.to_confirm)
    # 保留的必须是**原值**，不得被改写成半个合法键。
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "purchase_type")
    assert entry["attribute_value"] == "想买但还没定"
    assert entry["reasoning_detail"]["confidence"] == 0.5


def test_partially_illegal_enum_keeps_the_normalizable_part():
    """多选字段里部分能归一时，**不因一项不合法丢掉整个字段**。"""
    result = proj.project([claim("core_concerns", "油耗||看不懂的东西")])
    assert result.facts.get("core_concerns") == ["fuel_efficiency"]
    assert any(d["rule"] == "d_enum_partial" for d in result.downgraded)


def test_social_alone_downgrades_identity_field():
    """社交自呈现单独撑身份字段 → 降级（硬规则 3，与规则 b 同机制、不同规则名）。"""
    result = proj.project([claim("company", "某某公司", channel="小红书主页截图信息")])
    assert result.facts.get("company") == "某某公司"
    assert result.basis.get("company") == "inferred_low"
    assert any(d["rule"] == "b_social_only" for d in result.downgraded)


def test_attitude_field_without_firsthand_downgrades():
    """态度类字段只有弱来源 → 降级（硬规则 2）。"""
    result = proj.project([claim("emotion", "hate_being_rushed", channel="朋友圈截图内容信息")])
    assert result.facts.get("emotion") == ["hate_being_rushed"]
    assert result.basis.get("emotion") == "inferred_low"
    assert any(d["rule"] == "b_attitude_weak_source" for d in result.downgraded)


def test_prediction_channel_claiming_fact_is_capped_at_inferred_high():
    """渠道是第三方预测却自称 ``fact`` → 不得冒充事实，但按高置信推断处理。

    来源档比自述档硬：预测渠道来的结论永远不是 ``fact``（不是客户原话）。但 v3.2 起也
    不再压到 ``inferred_low``——采购的画像标签单独支撑某字段时按 ``inferred_high``。
    """
    result = proj.project(
        [claim("occupation", "小车司机", channel="第三方画像数据", basis="fact")]
    )
    assert result.basis.get("occupation") == "inferred_high"


# ---------------------------------------------------------------- 兜底：候选集与越界拒收

def test_l2_candidates_carry_source_and_basis():
    """兜底输入必须是**带来源档与 basis 的候选值**，否则它无从判断「谁更强」。"""
    result = proj.project(
        [
            claim("occupation", "教师", channel="外呼录音信息", created_at="2026-09-01"),
            claim("occupation", "医生", channel="外呼录音信息", created_at="2026-09-02"),
            claim("occupation", "货车司机", channel="第三方画像数据"),
        ]
    )
    candidates = proj.build_l2_candidates(
        [
            claim("occupation", "教师", channel="外呼录音信息", created_at="2026-09-01"),
            claim("occupation", "医生", channel="外呼录音信息", created_at="2026-09-02"),
            claim("occupation", "货车司机", channel="第三方画像数据"),
        ],
        result,
    )
    entry = next(c for c in candidates if c["attribute_key"] == "occupation")
    assert len(entry["options"]) == 3
    assert {o["tier"] for o in entry["options"]} == {"一手交互", "第三方预测"}
    # v3.2：第三方预测档的候选按 ``inferred_high`` 出现（此前为 inferred_low）。
    assert {o["basis"] for o in entry["options"]} >= {"fact", "inferred_high"}


def test_l2_verdict_cannot_add_value_outside_candidates():
    """兜底是**裁决者不是作者**：候选集外的取值一律拒收，facts 不动。"""
    candidates = [
        {"attribute_key": "occupation", "candidates": ["教师", "医生"], "options": []}
    ]
    out = proj.apply_l2_verdict(
        candidates,
        json.dumps(
            {
                "verdicts": [
                    {"attribute_key": "occupation", "value": "教师", "reason": "两条一手同向"},
                    {"attribute_key": "occupation", "value": "航天员", "reason": "编的"},
                ]
            },
            ensure_ascii=False,
        ),
    )
    assert [a["attribute_key"] for a in out["accepted"]] == ["occupation"]
    assert out["accepted"][0]["attribute_value"] == "教师"
    assert out["out_of_bounds"] == 1
    assert out["rejected"][0]["rule"] == "g_out_of_candidates"


def test_l2_verdict_accepts_value_from_options_too():
    """候选出处可以是 ``options``（带来源档的那份），不必只在 ``candidates`` 里。"""
    candidates = [
        {
            "attribute_key": "occupation",
            "candidates": [],
            "options": [{"value": "货车司机", "tier": "第三方预测", "basis": "inferred_low"}],
        }
    ]
    out = proj.apply_l2_verdict(
        candidates, json.dumps({"verdicts": [{"attribute_key": "occupation", "value": "货车司机"}]})
    )
    assert out["out_of_bounds"] == 0 and len(out["accepted"]) == 1


def test_l2_verdict_unparsable_is_reported():
    out = proj.apply_l2_verdict([], "这不是 JSON")
    assert out["parse_ok"] is False and out["accepted"] == []


def test_enum_key_passthrough():
    """合法枚举键原样直通。

    v2 契约把 ``purchase_type`` 的取值集缩到「个户 / 公户」，购车性质（首购 / 增购 /
    换购）改由编外字段 ``purchase_nature`` 承接，两者都要能直通。
    """
    assert proj.project([claim("purchase_type", "personal")]).facts.get("purchase_type") == "personal"
    assert proj.project([claim("purchase_nature", "additional")]).facts.get("purchase_nature") == "additional"


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
    """旧车证据 ⇒ 换购。

    落点是**编外字段** ``purchase_nature``：v2 契约的 ``purchase_type`` 只剩
    「个户 / 公户」，把 ``replacement`` 写进去既非法、又会在回传时被静默丢弃。
    """
    result = proj.project(
        [
            claim("old_car_model", "2019 别克"),
            claim("purchase_nature", "first"),
        ]
    )
    assert result.facts.get("purchase_nature") == "replacement"


def test_trade_in_yes_implies_replacement():
    """``trade_in_requirement=yes`` 本身就是「有旧车」的凭据 → 购买性质定为换购。"""
    result = proj.project(
        [claim("trade_in_requirement", '{"key":"yes","input_value":"现代"}')],
        context={"has_old_car_evidence": False},
    )
    assert result.facts.get("trade_in_requirement") == {"key": "yes", "input_value": "现代"}
    assert result.facts.get("purchase_nature") == "replacement"


def test_replacement_without_evidence_is_withdrawn():
    result = proj.project(
        [claim("purchase_nature", "replacement")],
        context={},
    )
    assert "purchase_nature" not in result.facts
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
    # v3.5：姓名恒有交代（这里没有姓名断言，故为「未知」）。
    assert keys == {"occupation", "customer_name"}
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
    assert proj.normalize_level("H") == "H"
    assert proj.normalize_level("h级") == "H"
    # 待识别是规范的独立结论，不是 C 级（R08）
    assert proj.normalize_level("待识别") == "待识别"
    assert proj.normalize_level("未知") == "待识别"


def test_split_multi_handles_list_and_string():
    assert proj.split_multi("a||b") == ["a", "b"]
    assert proj.split_multi(["a", "b||c"]) == ["a", "b", "c"]
    assert proj.split_multi("single") == ["single"]


# ---------------------------------------------------------------- h 落库序列化（R0 §5.2/契约）

def test_multi_value_enum_serialized_with_double_pipe():
    """多选枚举落库恒为 ``a||b`` 字符串（基线 18 个字段用此形态）。

    原用例取样 ``emotion``，它本轮已挪进编外字段、不再进 Notes 出口，
    因此改用同样多选的契约字段 ``core_concerns``（落库前先归一到英文键）。
    """
    result = proj.project([claim("core_concerns", "油耗||安全")])
    entry = next(u for u in result.notes_updates if u["attribute_key"] == "core_concerns")
    assert entry["attribute_value"] == "fuel_efficiency||safety"
    assert isinstance(entry["attribute_value"], str)


def test_internal_only_fields_never_reach_notes_updates():
    """编外字段照常进 ``facts``，但**不得**出现在 Notes 出口。

    这是「编外」二字的全部含义，也是日后一次重构最容易悄悄破坏的一条：
    :func:`projection._to_notes_updates` 若改成遍历 ``facts`` 而不是遍历契约键，
    编外字段就会原样回传，而回传体里多出一个契约没有的 ``attribute_key``，
    服务端反应不可预期。**只对 ``facts`` 断言是查不出这个的**，必须断言出口集合。
    """
    result = proj.project(
        [
            claim("emotion", "straightforward||hate_being_rushed"),
            claim("old_car_model", "2019 别克"),
            claim("core_concerns", "油耗||安全"),
        ]
    )
    # 取证侧：编外字段必须照常落进 facts，否则 purchase_decisions 与画像卡没有料
    assert result.facts.get("emotion") == ["straightforward", "hate_being_rushed"]
    assert result.facts.get("old_car_model") == "2019 别克"
    # 回传侧：只有契约字段能出去（姓名恒有交代，取值为「未知」）
    assert {u["attribute_key"] for u in result.notes_updates} == {"core_concerns", "customer_name"}


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
        {
            "hobbies",
            "pain_points",
            "special_requirements",
            "intended_brand",
            "intended_model",
            "competing_brand",
            "competing_model",
            "test_driven_models",
            "brand_concerns",
        }
    )
    # 集合内的字段都是契约里的自由文本字段（无 enum_options），否则归类无意义。
    assert all(not nc.is_enum(k) for k in proj.AGGREGATE_TEXT_KEYS)


# ---------------------------------------------------------------- v2.1 可读性硬约束

AUDIT_WORDS = (
    "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
    "未知渠道", "社交自呈现", "一手交互", "第三方预测", "公开登记查询", "人工/系统登记",
    "按来源分级投影得到", "断言",
)


def test_notes_reasoning_is_free_of_audit_language():
    """对客的 ``summary`` / ``steps`` / ``evidence`` 不得出现任何内部审计词（设计稿 §1）。"""
    result = proj.project([claim("occupation", "教师", quote="我以前做充电桩的")])
    blob = json.dumps([u["reasoning_detail"] for u in result.notes_updates], ensure_ascii=False)
    assert [w for w in AUDIT_WORDS if w in blob] == []


def test_evidence_prefers_verbatim_quote():
    """证据锚点首选**原文切片**，而不是来源档或字段名。"""
    result = proj.project([claim("occupation", "教师", quote="我以前做充电桩的")])
    detail = next(u for u in result.notes_updates if u["attribute_key"] == "occupation")["reasoning_detail"]
    assert any("我以前做充电桩的" in str(e) for e in detail["evidence"])


def test_plain_text_strips_tier_labels_and_internal_confidence():
    """``_plain_text`` 是出口自净：模型把来源档代号写进推断依据时，在此剥除。"""
    assert proj._plain_text("第三方预测性别为男（0.94）") == "性别为男"
    stripped = proj._plain_text("来源分级：取最高档 社交自呈现")
    assert "社交自呈现" not in stripped and "来源分级" not in stripped


def test_inferred_notes_never_quote_tier_labels():
    """实测缺陷：L1 把「第三方预测…」写进 ``basis_of_inference``，出口必须剥掉。"""
    result = proj.project(
        [
            claim(
                "annual_income",
                "30-50万",
                basis="inferred_high",
                basis_of_inference=("第三方预测年收入 30-50 万（0.9）", "关注 30 万以上车型"),
            )
        ]
    )
    blob = json.dumps([u["reasoning_detail"] for u in result.notes_updates], ensure_ascii=False)
    assert "第三方预测" not in blob
    assert "0.9" not in blob


# ---------------------------------------------------------------- 收入档位换算

def test_income_band_maps_to_range_in_facts_and_notes():
    """第三方「N档收入」进 facts 与 Notes 时都是金额区间，不是档位原文。"""
    result = proj.project(
        [claim("annual_income", "5档收入", channel="第三方画像数据", basis="inferred_high",
               basis_of_inference=("第三方预测收入水平",))]
    )
    assert result.facts["annual_income"] == "40-60万"
    note = next(u for u in result.notes_updates if u["attribute_key"] == "annual_income")
    assert note["attribute_value"] == "40-60万"
    assert "档收入" not in note["attribute_value"]


def test_all_six_income_bands_have_ranges():
    expected = {"1档收入": "8-15万", "2档收入": "10-20万", "3档收入": "20-30万",
                "4档收入": "30-40万", "5档收入": "40-60万", "6档收入": "60万以上"}
    for band, rng in expected.items():
        assert proj.normalize_income(band) == rng


def test_income_amounts_are_not_rewritten():
    """客户自述或模型推断的金额不是档位，必须原样透传。"""
    for value in ("30-50万", "10-20万", "8w-15w", "60万以上"):
        assert proj.normalize_income(value) is None
    result = proj.project([claim("annual_income", "30-50万", basis="inferred_high",
                                 basis_of_inference=("关注 30 万以上车型",))])
    assert result.facts["annual_income"] == "30-50万"


def test_income_band_is_normalized_in_serialized_output():
    """L2 兜底给的档位原文也在出口收口（它不经过 ``project()`` 的落值分支）。"""
    assert proj.serialize_value("annual_income", "4档收入") == "30-40万"


# ---------------------------------------------------------------- R2 缺字段候选

def test_candidates_include_missing_contract_fields():
    """L1 没给结论的契约字段要成为缺字段候选，好让兜底基于 facts 补一个软值。"""
    ledger = [claim("occupation", "教师")]
    candidates = proj.build_l2_candidates(ledger, proj.project(ledger), covered={"occupation"})
    missing = {c["attribute_key"] for c in candidates if c.get("missing")}
    assert missing, "缺字段候选不得为空"
    assert "occupation" not in missing, "L1 已给结论的字段不应被当成缺字段"
    for entry in candidates:
        if entry.get("missing"):
            assert entry["candidates"] == [] and entry["options"] == []


def test_missing_field_backfill_is_forced_soft():
    """缺字段补全恒为 ``inferred_low``——即便模型自称 ``inferred_high``。"""
    candidates = [
        {"attribute_key": "annual_income", "candidates": [], "options": [], "missing": True}
    ]
    out = proj.apply_l2_verdict(
        candidates,
        json.dumps(
            {
                "verdicts": [
                    {
                        "attribute_key": "annual_income",
                        "value": "15-25万",
                        "basis": "inferred_high",
                        "reason": "据职业为教师归纳",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    assert out["out_of_bounds"] == 0
    assert out["accepted"][0]["basis"] == "inferred_low"
    assert out["accepted"][0]["missing"] is True


# ---------------------------------------------------------------- v3 接口一：坐标形态

def test_normalized_fact_evidence_shows_source_quote():
    """当归型事实（ASR 纠错/归一）的证据须呈现**原文**，让人分清「客户说的」与「模型改的」。"""
    detail = proj._readable_evidence(
        [
            proj.Claim(
                attribute="intended_model",
                value="埃安",
                channel="试驾录音信息",
                anchor_kind=proj.ANCHOR_NORMALIZED,
                source_quote="安安I60",
            )
        ]
    )
    joined = " ".join(detail)
    assert "安安I60" in joined, f"当归型事实的证据须含原文，实际 {detail}"
    assert "埃安" not in joined or "安安" in joined


def test_verbatim_fact_uses_quote_not_source_quote():
    """锚定型事实优先用 ``quote``（原文切片）。"""
    detail = proj._readable_evidence(
        [
            proj.Claim(
                attribute="old_car_model",
                value="2019款别克君威",
                channel="外呼录音信息",
                quote="我现在开的是一九年的君威",
                anchor_kind=proj.ANCHOR_VERBATIM,
            )
        ]
    )
    assert any("我现在开的是一九年的君威" in d for d in detail)


# ---------------------------------------------------------------- v3 接口一：结论型依据剔除

def test_conclusion_only_hint_is_stripped_from_evidence():
    """证据位不得出现结论型自指；中段的「，推断为…」从句要被裁掉，保留其前的事实部分。"""
    detail = proj._readable_evidence(
        [
            proj.Claim(
                attribute="personality_and_preferences",
                value="实用型",
                channel="",
                basis=proj.INFERRED_HIGH,
                basis_of_inference=(
                    "客户主动询问车型优惠价格及置换补贴，关注实际购车成本，推断为实用型",
                ),
            )
        ]
    )
    joined = " ".join(detail)
    assert "推断为" not in joined, f"结论型从句须剔除，实际 {detail}"
    assert "关注实际购车成本" in joined, f"事实部分须保留，实际 {detail}"


def test_pure_conclusion_hint_is_dropped_entirely():
    """整段皆为结论的依据（「推断为40-49岁」）应整条丢弃，不留占位。"""
    assert proj._strip_conclusion("推断为40-49岁") == ""
    detail = proj._readable_evidence(
        [
            proj.Claim(
                attribute="age",
                value="40-49岁",
                channel="",
                basis=proj.INFERRED_HIGH,
                basis_of_inference=("推断为40-49岁",),
            )
        ]
    )
    assert not any("推断" in d for d in detail)


def test_confidence_token_is_stripped_from_plain_text():
    """内部置信度串（「置信度0.9377」）不得出现在对客文案里。"""
    assert "0.9377" not in proj._plain_text("性别为男，置信度0.9377")
    assert "0.94" not in proj._plain_text("预测性别为男（0.94）")


# ---------------------------------------------------------------- v3 接口二：身份字段来源白名单

def test_customer_name_from_social_source_is_rejected():
    """社交自呈现的昵称/账号名不得写成客户姓名。"""
    result = proj.project(
        [claim("customer_name", "清CN欢", channel="微信手机号搜索界面信息")]
    )
    assert result.facts.get("customer_name") == "未知"
    assert any(r["rule"] == "name_source_whitelist" for r in result.rejected)


def test_customer_name_from_business_registry_is_rejected():
    """营业执照主体名不得写成客户姓名（v2.1 实测「…新泽水果店」）。"""
    result = proj.project(
        [claim("customer_name", "汕头市潮南区井都新泽水果店", channel="关联企业信息")]
    )
    assert result.facts.get("customer_name") == "未知"
    assert any(r["rule"] == "name_source_whitelist" for r in result.rejected)


def test_customer_name_from_firsthand_is_kept():
    """一手自述的姓名应保留。"""
    result = proj.project(
        [claim("customer_name", "陈", channel="试驾录音信息")]
    )
    assert result.facts.get("customer_name") == "陈"


def test_customer_name_whitelist_filters_before_picking():
    """白名单来源与社交来源并存时，只让白名单那一条参与裁决。"""
    result = proj.project(
        [
            claim("customer_name", "陈", channel="试驾录音信息", basis="fact"),
            claim("customer_name", "清CN欢", channel="微信手机号搜索界面信息", basis="fact"),
        ]
    )
    assert result.facts.get("customer_name") == "陈"


def test_title_from_recording_is_downgraded_not_fact():
    """性别/称呼来自录音或头像标签时只能降为 ``inferred_low``，不得当成确定值。"""
    result = proj.project([claim("title", "Mr", channel="试驾录音信息")])
    assert result.facts.get("title") == "Mr"
    assert result.basis.get("title") == proj.INFERRED_LOW


def test_title_from_registry_is_kept():
    """登记称呼不受该降级影响。"""
    result = proj.project([claim("title", "Ms", channel="销售人员人工确认的客户Notes信息")])
    assert result.basis.get("title") != proj.INFERRED_LOW


# ------------------------------------------- a2 值形态门（v3.1 改造二）

def test_salesperson_value_rejected_from_customer_name():
    """取值明写「顾问自称」的姓名不得进客户姓名位（v3.1 实测 13435844484）。

    该条断言的 ``subject`` 被模型写成 ``customer``，归属过滤拦不住——只有值形态门
    看取值本身说了什么，才能拦下它。
    """
    result = proj.project(
        [
            claim(
                "customer_name",
                "郑小姐（顾问自称，非客户姓名）",
                channel="外呼录音信息",
                subject="customer",
            )
        ]
    )
    assert result.facts.get("customer_name") == "未知"
    assert any(r["rule"] == "a_salesperson_value" for r in result.rejected)


def test_salesperson_occupation_rejected():
    """「销售顾问」这一职业归属我方人员，不得写成客户的职业。"""
    result = proj.project([claim("occupation", "汽车销售顾问", channel="试驾录音信息")])
    assert "occupation" not in result.facts
    assert any(r["rule"] == "a_salesperson_value" for r in result.rejected)


def test_salesperson_gate_ignores_basis_of_inference():
    """值形态门只看取值与锚点，**不看**推断依据。

    推断依据里出现「销售以『大哥』称呼对方」是**正常**的（说话人是谁本就常靠销售的话
    判断），把它算进词表会误杀大量正确的推断。
    """
    result = proj.project(
        [
            claim(
                "title",
                "Mr",
                channel="试驾录音信息",
                basis="inferred_low",
                basis_of_inference=("销售以『大哥』称呼对方",),
            )
        ]
    )
    assert result.facts.get("title") == "Mr"


def test_salesperson_gate_exempts_registry():
    """豁免登记档：人工确认的客户职业确实可能就叫「销售顾问」。"""
    result = proj.project(
        [claim("occupation", "销售顾问", channel="销售人员人工确认的客户Notes信息")]
    )
    assert result.facts.get("occupation") == "销售顾问"


def test_salesperson_gate_does_not_touch_non_identity_field():
    """值形态门只对身份字段生效，非身份字段不受影响。"""
    result = proj.project(
        [claim("special_requirements", "希望销售顾问上门交车", channel="外呼录音信息")]
    )
    assert result.facts.get("special_requirements") == "希望销售顾问上门交车"


def test_customer_own_occupation_survives():
    """客户自己说「我是做销售的」不得被误杀——词表只收精指向词。"""
    result = proj.project([claim("occupation", "做销售的", channel="试驾录音信息")])
    assert result.facts.get("occupation") == "做销售的"


# ------------------------------------------- v3.5 置信度口径 / 姓名兜底

def _note(result, key):
    return next(u for u in result.notes_updates if u["attribute_key"] == key)


def test_phone_number_is_full_confidence():
    """手机号直接取自原始数据，恒 1.0（用户口径）。"""
    result = proj.project([claim("phone_number", "15308826588", channel="客户留资手机号")])
    assert _note(result, "phone_number")["reasoning_detail"]["confidence"] == 1.0


def test_wechat_id_read_from_screenshot_is_full_confidence():
    """微信号能从截图中明确识别（有原文切片）时给 1.0。"""
    result = proj.project(
        [claim("wechat_id", "wx_abc123", channel="微信主页截图信息", quote="微信号：wx_abc123")]
    )
    assert _note(result, "wechat_id")["reasoning_detail"]["confidence"] == 1.0


def test_fact_confidence_follows_model_self_report():
    """事实档不再恒为 0.7：按 L1 自报分校准到 0.85–0.95。"""
    result = proj.project(
        [claim("intended_model", "埃安A27", channel="外呼录音信息", confidence=0.8)]
    )
    value = _note(result, "intended_model")["reasoning_detail"]["confidence"]
    assert 0.85 <= value <= 0.95


def test_inferred_low_keeps_low_self_report():
    """低置信推断按自报分落在 0.40–0.60；自报低于 0.4 的纯猜测落在 0.20–0.30。"""
    result = proj.project(
        [claim("pain_points", "不喜欢被催促", channel="朋友圈截图内容信息", confidence=0.55)]
    )
    assert _note(result, "pain_points")["reasoning_detail"]["confidence"] == 0.55

    guessed = proj.project(
        [
            claim(
                "pain_points",
                "不喜欢被催促",
                channel="朋友圈截图内容信息",
                confidence=0.25,
            )
        ]
    )
    value = _note(guessed, "pain_points")["reasoning_detail"]["confidence"]
    assert 0.2 <= value <= 0.3


def test_multi_channel_convergence_gives_high_confidence():
    """多条不同渠道同向的高可信结论给 0.95（用户口径：极高可信）。"""
    result = proj.project(
        [
            claim("occupation", "珠宝零售", channel="关联企业信息", basis="inferred_high", confidence=0.7),
            claim("occupation", "珠宝零售", channel="外呼录音信息", basis="inferred_high", confidence=0.7),
        ]
    )
    assert result.basis.get("occupation") == proj.INFERRED_HIGH
    assert _note(result, "occupation")["reasoning_detail"]["confidence"] == 0.95


def test_salutation_never_becomes_customer_name():
    """称呼（先生/女士/大哥）即使来自一手来源也不得写成姓名。"""
    result = proj.project([claim("customer_name", "先生", channel="外呼录音信息")])
    assert result.facts.get("customer_name") == "未知"
    assert any(r["rule"] == "name_salutation_value" for r in result.rejected)


def test_unknown_name_note_explains_absence():
    """姓名「未知」的 Notes 必须说清为什么没有，而不是含糊的「据现有记录归纳」。"""
    result = proj.project([claim("occupation", "教师", channel="试驾录音信息")])
    note = _note(result, "customer_name")
    assert note["attribute_value"] == "未知"
    assert note["reasoning_detail"]["summary"] == "客户信息中未查找到真实姓名"
    assert note["reasoning_detail"]["confidence"] == 1.0


def test_unknown_name_falls_back_to_phone_suffix():
    """姓名查不到但有手机号时，兜底输出「客户」+后四位（v3.5，2026-10-20 用户口径）。"""
    result = proj.project(
        [
            claim("occupation", "教师", channel="试驾录音信息"),
            claim("phone_number", "15308826588", channel="客户留资手机号"),
        ]
    )
    assert result.facts.get("customer_name") == "客户6588"
    note = _note(result, "customer_name")
    assert note["attribute_value"] == "客户6588"
    # 兜底值是确定结论，不是猜测：置信度仍为 1.0，Notes 说明与「未知」同口径。
    assert note["reasoning_detail"]["summary"] == "客户信息中未查找到真实姓名"
    assert note["reasoning_detail"]["confidence"] == 1.0


def test_real_name_is_never_replaced_by_phone_suffix():
    """有可靠姓名时不受兜底影响。"""
    result = proj.project(
        [
            claim("customer_name", "陈", channel="试驾录音信息"),
            claim("phone_number", "15308826588", channel="客户留资手机号"),
        ]
    )
    assert result.facts.get("customer_name") == "陈"


def test_unresolved_customer_name_without_phone_is_unknown():
    """手机号缺失或不足四位时退回「未知」。"""
    assert proj.unresolved_customer_name("") == "未知"
    assert proj.unresolved_customer_name("123") == "未知"
    assert proj.unresolved_customer_name("15308826588") == "客户6588"


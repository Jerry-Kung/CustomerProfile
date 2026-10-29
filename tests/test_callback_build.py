"""`callback_build`：v2 回传体构造的契约回归。

**为什么这层要单独钉住**：``analysis_result`` 的四个区块是**全新产出**，没有历史批次
可比（方案 R3），质量只能靠人读；但它的**结构约束是硬的、且错了不报错**——

- 分类缺料时写成 ``items: []`` 而不是整个分类不出现，会**清空**客户已有的该分类数据
  （接口文档 §1：区块缺失保留原值，显式空数组才清空）；
- ``id`` 撞号会让整次回传递交失败（``(customer_phone, item_id)`` 唯一）；
- 编外字段漏进 ``basic_notes_updates``，服务端反应不可预期。

这三条都不会在本地冒烟时显形，只会在真实回传那一刻出事。因此逐条用例钉住。
"""

from __future__ import annotations

import json

import pytest

from customer_profile import callback_build as cb
from customer_profile import internal_fields as inf
from customer_profile import notes_contract as nc


# ---------------------------------------------------------------- 夹具

def _facts(**pairs) -> str:
    return json.dumps(pairs, ensure_ascii=False)


def _notes(*entries) -> str:
    return json.dumps(list(entries), ensure_ascii=False)


def _express(**sections) -> str:
    return json.dumps({"analysis_result": sections}, ensure_ascii=False)


def _build(facts="{}", notes="[]", express="{}", claims="{}"):
    return json.loads(
        cb.build_analysis_result("13500000000", "b-1", facts, notes, express, claims)
    )


def _categories(result) -> dict[str, list[dict]]:
    """分类表。整个 ``purchase_decisions`` 缺失时给空表。

    **不给默认空表就等于断言「一定有这个区块」**——而缺料时整个区块不出现才是契约
    要求的语义（见 :func:`test_empty_category_is_omitted_not_sent_as_empty_array`），
    因此这里必须容忍它缺席，否则用例会在正确的实现上炸 KeyError。
    """
    return {
        block["category"]: block["items"]
        for block in result["analysis_result"].get("purchase_decisions") or []
    }


# ---------------------------------------------------------------- 顶层形状

def test_top_level_is_exactly_three_keys():
    """顶层恒为 ``phone`` / ``batch_id`` / ``analysis_result``（文档 §1，L-003 的兑现点）。"""
    payload = _build(facts=_facts(purchase_type="personal"))
    assert set(payload) == {"phone", "batch_id", "analysis_result"}
    assert payload["phone"] == "13500000000"
    assert payload["batch_id"] == "b-1"


def test_analysis_result_never_exceeds_the_six_contract_keys():
    """``analysis_result`` 的键恒是契约六键的子集——多一个键就是契约外的载荷。"""
    payload = _build(
        facts=_facts(core_concerns="油耗", old_car_model="2019款别克君威"),
        express=_express(resistances=[{"resistance_name": "价格不透明"}]),
    )
    keys = set(payload["analysis_result"])
    assert keys <= {
        "profile_summary",
        "basic_notes_updates",
        "ai_insight",
        "purchase_decisions",
        "deal_battle",
        "decision_psychology",
    }
    # 两个基础键恒在
    assert {"profile_summary", "basic_notes_updates"} <= keys


def test_result_is_a_json_string():
    """节点出参是 JSON 串（下游 ``@body_ref`` 直接当请求体用）。"""
    out = cb.build_analysis_result()
    assert isinstance(out, str)
    assert isinstance(json.loads(out), dict)
    assert cb.main() == {"callback_json": out}


# ---------------------------------------------------------------- profile_summary

def test_profile_summary_carries_only_name_and_gender():
    """新契约的 ``profile_summary`` **只剩** ``name`` / ``gender``。

    旧结构里的 ``age`` / ``deal_level`` / ``overall_tag`` 一律不得再从这里出去
    ——它们已改由 ``ai_insight`` 承载。这一条最容易被「顺手把事实表全抄进来」破坏。
    """
    payload = _build(facts=_facts(customer_name="陈", title="Ms", age="40 ~ 49岁", deal_level="C"))
    summary = payload["analysis_result"]["profile_summary"]
    assert summary == {"name": "陈", "gender": "女"}


def test_profile_summary_omits_absent_fields():
    """未识别不覆盖现有值：取不到就整个键不给，而不是给空串。"""
    payload = _build(facts=_facts(occupation="教师"))
    assert payload["analysis_result"]["profile_summary"] == {}


def test_gender_comes_from_title_not_copied_verbatim():
    """``title`` 的 ``Mr`` / ``Ms`` 要映射成「男 / 女」，不能照抄键名。"""
    assert _build(facts=_facts(title="Mr"))["analysis_result"]["profile_summary"]["gender"] == "男"
    assert _build(facts=_facts(title="unknown"))["analysis_result"]["profile_summary"] == {}


# ---------------------------------------------------------------- basic_notes_updates

def test_internal_only_fields_never_enter_basic_notes_updates():
    """编外字段是**白名单之外**：Notes 出口只走契约键。

    这是「编外」二字的全部含义。``company`` / ``purchase_nature`` / ``old_car_model``
    都在 facts 里，也都在内部推理里有用，但回传体里多一个契约没有的 ``attribute_key``
    反应不可预期。
    """
    notes = _notes(
        {"attribute_key": "occupation", "attribute_value": "教师"},
        {"attribute_key": "company", "attribute_value": "某某电力局"},
        {"attribute_key": "purchase_nature", "attribute_value": "replacement"},
        {"attribute_key": "old_car_model", "attribute_value": "2019款别克君威"},
    )
    payload = _build(notes=notes)
    keys = [u["attribute_key"] for u in payload["analysis_result"]["basic_notes_updates"]]
    assert keys == ["occupation"]
    assert set(keys) <= set(inf.CALLBACK_KEYS)


def test_basic_notes_updates_keeps_reasoning_detail_shape():
    """Notes 那一条的 ``reasoning_detail`` 到回传体仍是四键（契约只认这个形状）。"""
    notes = _notes(
        {
            "attribute_key": "occupation",
            "attribute_value": "教师",
            "reasoning_detail": {"summary": "客户自称", "steps": ["自称老师"], "evidence": [], "confidence": 0.9},
        }
    )
    entry = _build(notes=notes)["analysis_result"]["basic_notes_updates"][0]
    assert set(entry["reasoning_detail"]) == {"summary", "steps", "evidence", "confidence"}
    assert entry["attribute_value"] == "教师"


def test_consumption_level_summary_carries_band_note():
    """SPEC-16 正例：消费等级的「AI 分析总结」末尾固定拼接档位说明，且不重复叠加。"""
    note = "（消费等级分6档，数字越大代表消费水平越高）"
    notes = _notes(
        {
            "attribute_key": "consumption_level",
            "attribute_value": "level_3",
            "reasoning_detail": {"summary": "第三方标签给出 3 档消费", "steps": [], "evidence": [], "confidence": 0.8},
        }
    )
    first = cb.build_basic_notes_updates(json.loads(notes))
    assert first[0]["reasoning_detail"]["summary"] == "第三方标签给出 3 档消费" + note

    # 回写结果再走一遍出口（离线重放/二次回写）不得叠加第二句。
    again = cb.build_basic_notes_updates(first)
    assert again[0]["reasoning_detail"]["summary"] == first[0]["reasoning_detail"]["summary"]


def test_other_fields_summary_has_no_band_note():
    """SPEC-16 反例：档位说明只贴消费等级，不污染其它字段的分析总结。"""
    notes = _notes(
        {
            "attribute_key": "occupation",
            "attribute_value": "教师",
            "reasoning_detail": {"summary": "客户自称", "steps": [], "evidence": [], "confidence": 0.9},
        }
    )
    entry = cb.build_basic_notes_updates(json.loads(notes))[0]
    assert entry["reasoning_detail"]["summary"] == "客户自称"


# ---------------------------------------------------------------- ai_insight

def test_purchase_intent_level_is_normalized_without_the_suffix():
    """意向等级取自 ``deal_level`` 并归一，**不含「级」字**；取值按定级规范原样回传。"""
    value = _build(facts=_facts(deal_level="C级"))["analysis_result"]["ai_insight"]
    assert value["purchase_intent_level"]["value"] == "C"


def test_purchase_intent_level_absent_when_facts_have_no_level():
    """事实表没有等级时整个项不出现——不给一个 ``value: ""`` 的死值。"""
    payload = _build(facts=_facts(occupation="教师"))
    assert "ai_insight" not in payload["analysis_result"]


def test_customer_segment_reads_the_expression_layer():
    """``customer_segment`` 取表达层自由生成的 ``overall_tag``（本轮不约束口径，见 L-001）。"""
    payload = _build(express=_express(profile_summary={"overall_tag": "高效务实中产"}))
    segment = payload["analysis_result"]["ai_insight"]["customer_segment"]
    assert segment["value"] == "高效务实中产"
    assert set(segment["reasoning_detail"]) == {"summary", "steps", "evidence", "confidence"}


# ---------------------------------------------------------------- purchase_decisions

def test_six_categories_keep_the_contract_order():
    """六类的顺序即落库顺序，且**逐字**取自契约（前端按 ``category`` 映射图标）。"""
    assert cb.PURCHASE_CATEGORIES == (
        "到店动因", "当前用车", "换车动因", "对比车型", "核心关注", "决策阻力",
    )


def test_empty_category_is_omitted_not_sent_as_empty_array():
    """缺料的分类**整个不出现**。

    契约语义是「区块缺失保留原值，显式空数组才清空」——给一个 ``items: []`` 会把
    客户已有的该分类数据清掉。这是四条结构约束里最隐蔽的一条：本地看产物人眼觉得
    「结构完整」，线上却是数据丢失。

    例外是「换车动因」与「对比车型」：两类缺料时给一条「暂无…」填充项，保卡片版式
    （见 :func:`test_change_and_compare_categories_get_a_filler_when_empty`）；
    其余四类缺料时连分类本身都不出现。
    """
    payload = _build(facts=_facts(purchase_type="personal"))  # 只在契约字段，无任何分块料
    categories = _categories(payload)
    assert set(categories) == {"换车动因", "对比车型"}
    for items in categories.values():
        assert len(items) == 1
        assert items[0]["label"].startswith("暂无")
    assert "deal_battle" not in payload["analysis_result"]
    assert "decision_psychology" not in payload["analysis_result"]


def test_change_and_compare_categories_get_a_filler_when_empty():
    """「暂无换车动因」「暂无对比车型」是**填充项**，不是伪造事实。

    - 文案是多字说明，不是单个「无」；
    - 置信度取低档（``< 0.5``），销售一眼能分辨这是「没查到」而不是「查到了没有」；
    - id 走内容摘要，同分类每次回传恒定。
    """
    first = _categories(_build(facts=_facts(purchase_type="personal")))
    second = _categories(_build(facts=_facts(purchase_type="personal")))
    assert first["换车动因"][0]["label"] == "暂无换车动因"
    assert first["对比车型"][0]["label"] == "暂无对比车型"
    for items in first.values():
        assert items[0]["reasoning_detail"]["confidence"] < 0.5
    assert first == second  # id 与内容稳定


def test_filler_does_not_replace_real_items():
    """有料时不得再塞填充项——「暂无」与真实条目不能同时出现。"""
    categories = _categories(
        _build(facts=_facts(competing_model="豹8", purchase_nature="replacement", pain_points="车太小"))
    )
    assert [i["label"] for i in categories["对比车型"]] == ["豹8"]
    assert "暂无换车动因" not in [i["label"] for i in categories["换车动因"]]


def test_item_ids_use_the_contract_prefixes():
    """id 前缀区分分类（契约 §2.4 建议），使 ``(phone, item_id)`` 唯一有结构保证。"""
    payload = _build(
        facts=_facts(
            purchase_purpose="family",
            old_car_model="2019款别克君威",
            purchase_nature="replacement",
            pain_points="车太小",
            competing_model="豹8",
            core_concerns="油耗",
            brand_concerns="保值率",
        )
    )
    categories = _categories(payload)
    prefixes = {
        "到店动因": "visit-",
        "当前用车": "vehicle-",
        "换车动因": "change-",
        "对比车型": "compare-",
        "核心关注": "focus-",
        "决策阻力": "barrier-",
    }
    for category, prefix in prefixes.items():
        for item in categories[category]:
            assert item["id"].startswith(prefix), item


def test_items_are_ranked_from_one_within_each_category():
    payload = _build(facts=_facts(core_concerns="油耗||安全"))
    items = _categories(payload)["核心关注"]
    assert [item["rank_order"] for item in items] == [1, 2]


def test_chinese_display_names_not_english_keys():
    """卡片是给销售看的：枚举取值要渲染成中文展示名，不写 ``fuel_efficiency``。"""
    items = _categories(_build(facts=_facts(core_concerns="fuel_efficiency")))["核心关注"]
    assert items[0]["label"] == "油耗"


def test_change_category_requires_replacement_nature():
    """「换车动因」只在 ``purchase_nature=replacement`` 时成立。

    首购客户没有换车动因；把他对别的事物的不满写进这一类会误导销售。``pain_points``
    是契约字段，本身不区分「对现有车不满」与「一般用车痛点」，故以购车性质为闸门。
    闸门不成立时该分类给「暂无换车动因」填充项，而不是把 ``pain_points`` 塞进去。
    """
    first_purchase = _categories(_build(facts=_facts(pain_points="车太小")))
    assert [i["label"] for i in first_purchase["换车动因"]] == ["暂无换车动因"]
    replacement = _categories(
        _build(facts=_facts(pain_points="车太小", purchase_nature="replacement"))
    )
    assert "车太小" in [i["label"] for i in replacement["换车动因"]]


def test_current_vehicle_does_not_repeat_the_same_car():
    """``current_vehicle_info`` 与 ``old_car_model`` 装着同一台车时只列一条。

    两处常从同一句「我现在开的是一九年的君威」里抽出，照单全收会让卡片上出现两条
    同内容的「当前用车」。
    """
    items = _categories(
        _build(facts=_facts(current_vehicle_info="2019款别克君威", old_car_model="2019款别克君威"))
    )["当前用车"]
    assert len(items) == 1
    assert items[0]["label"] == "2019款别克君威"


def test_current_vehicle_keeps_distinct_facts():
    """去重只吃「同内容」，不得把车龄这类**不同事实**一起吞掉。"""
    items = _categories(
        _build(
            facts=_facts(
                current_vehicle_info="有车（2019款别克君威）",
                old_car_model="2019款别克君威",
                owned_car_age="6年",
            )
        )
    )["当前用车"]
    labels = [item["label"] for item in items]
    assert "2019款别克君威" not in labels  # 已被 current_vehicle_info 包含，去重
    assert any("6年" in label for label in labels)  # 车龄是另一条事实，保留


def test_resistances_move_from_notes_to_barrier_and_loss():
    """``resistances`` 已离开 Notes，内容改走「决策阻力」与``loss_conditions``。"""
    express = _express(resistances=[{"resistance_name": "价格不透明", "severity": "高"}])
    payload = _build(express=express)
    result = payload["analysis_result"]
    assert "价格不透明" in [i["label"] for i in _categories(payload)["决策阻力"]]
    assert "价格不透明" in [i["label"] for i in result["deal_battle"]["loss_conditions"]]


def test_resistances_read_from_nested_overview():
    """表达层把 ``resistances`` 写成 ``customer_overview`` 的子键时也要认。"""
    express = _express(customer_overview={"resistances": [{"resistance_name": "担心续航"}]})
    items = _categories(_build(express=express))["决策阻力"]
    assert "担心续航" in [i["label"] for i in items]


# ---------------------------------------------------------------- deal_battle

def test_win_conditions_require_basic_knowledge_at_least():
    """品牌认知门：``none`` / ``unknown`` 不构成成交有利因素，``basic`` / ``deep`` 才算。"""
    assert "deal_battle" not in _build(facts=_facts(knowledge_of_mengshi="none"))["analysis_result"]
    assert "deal_battle" not in _build(facts=_facts(knowledge_of_mengshi="unknown"))["analysis_result"]
    win = _build(facts=_facts(knowledge_of_mengshi="basic"))["analysis_result"]["deal_battle"]
    assert "win_conditions" in win


def test_win_conditions_require_a_near_purchase_window():
    """时间窗门：三个月内及更近才算紧迫度，``over_3_months`` 不构成有利因素。"""
    assert "deal_battle" not in _build(facts=_facts(planned_purchase_time="over_3_months"))["analysis_result"]
    win = _build(facts=_facts(planned_purchase_time="this_month"))["analysis_result"]["deal_battle"]
    assert any("本月" in i["label"] for i in win["win_conditions"])


def test_loss_conditions_include_competitors_and_credit():
    """竞品在 ``deal_battle`` 里是**风险信号**（与 ``purchase_decisions`` 里的中性信息不冲突）。"""
    loss = _build(
        facts=_facts(competing_model="豹8", credit_status="有逾期记录")
    )["analysis_result"]["deal_battle"]["loss_conditions"]
    labels = [i["label"] for i in loss]
    assert any("豹8" in label for label in labels)
    assert any("逾期" in label for label in labels)


def test_battle_arrays_have_the_same_element_shape():
    """两个数组元素结构相同：``id`` / ``label`` / ``reasoning_detail`` / ``rank_order``。"""
    battle = _build(facts=_facts(core_concerns="油耗", brand_concerns="保值率"))[
        "analysis_result"
    ]["deal_battle"]
    for key in ("win_conditions", "loss_conditions"):
        for item in battle[key]:
            assert set(item) == {"id", "label", "reasoning_detail", "rank_order"}
            assert item["id"].startswith("win-" if key == "win_conditions" else "loss-")


# ---------------------------------------------------------------- decision_psychology

def test_psychology_has_four_fixed_keys_in_contract_order():
    """四个固定 ``key``，顺序与契约一致；``label`` 用契约给的中文维度名。"""
    assert [key for key, _ in cb.PSYCHOLOGY_KEYS] == [
        "decision_style", "current_attitude", "communication_preference", "emotion_state",
    ]


def test_migrated_internal_fields_land_in_psychology():
    """三个迁移字段的落点：``personality_and_preferences`` / ``communication_rhythm`` / ``emotion``。

    「迁移」而非「舍弃」的含义就在这条用例里——它们离开 Notes 后，唯一还能被业务方
    看到的地方是这里。
    """
    entries = {
        e["key"]: e["value"]
        for e in _build(
            facts=_facts(
                personality_and_preferences="rational",
                communication_rhythm="prudent",
                emotion="hate_being_rushed",
                contact_preference="phone",
                viewing_progress="comparing",
            )
        )["analysis_result"]["decision_psychology"]
    }
    assert entries["decision_style"] == "理性型"
    assert "谨慎" in entries["communication_preference"] and "电话" in entries["communication_preference"]
    assert "讨厌被催" in entries["emotion_state"]
    assert "对比中" in entries["current_attitude"]


def test_psychology_supports_partial_results():
    """契约本就是「每客户 0~4 条」：只有一半线索时只返回那几条，不补空。"""
    entries = _build(facts=_facts(personality_and_preferences="rational"))[
        "analysis_result"
    ]["decision_psychology"]
    assert [e["key"] for e in entries] == ["decision_style"]
    assert entries[0]["rank_order"] == 1


# ---------------------------------------------------------------- reasoning_detail

def test_every_reasoning_detail_has_exactly_four_keys():
    """契约 §3：``summary`` / ``steps`` / ``evidence`` / ``confidence``。``evidence`` 可空。"""
    payload = _build(
        facts=_facts(
            deal_level="C",
            core_concerns="油耗",
            brand_concerns="保值率",
            personality_and_preferences="rational",
            competing_model="豹8",
            knowledge_of_mengshi="basic",
            planned_purchase_time="this_month",
        ),
        express=_express(
            profile_summary={"overall_tag": "务实中产"},
            resistances=[{"resistance_name": "价格不透明"}],
        ),
    )
    details = []

    insight = payload["analysis_result"]["ai_insight"]
    details.extend(item["reasoning_detail"] for item in insight.values())
    for block in payload["analysis_result"]["purchase_decisions"]:
        details.extend(item["reasoning_detail"] for item in block["items"])
    for key in ("win_conditions", "loss_conditions"):
        details.extend(
            item["reasoning_detail"] for item in payload["analysis_result"]["deal_battle"][key]
        )
    details.extend(
        entry["reasoning_detail"] for entry in payload["analysis_result"]["decision_psychology"]
    )

    assert details
    for detail in details:
        assert set(detail) == {"summary", "steps", "evidence", "confidence"}
        assert 0.0 <= detail["confidence"] <= 1.0


def test_detail_reuses_the_notes_wording_for_the_same_field():
    """同一字段的 ``reasoning_detail`` 复用 Notes 那一条的措辞。

    四个区块若各自另写一套，同一事实在卡片与 Notes 上就会有两种说法——那正是
    production 图「卡片说 A、Notes 说 B」的老毛病换个地方出现。
    """
    notes = _notes(
        {
            "attribute_key": "core_concerns",
            "attribute_value": "fuel_efficiency",
            "reasoning_detail": {
                "summary": "客户反复问油耗",
                "steps": ["问了几次油耗"],
                "evidence": ["我比较看重油耗"],
                "confidence": 0.9,
            },
        }
    )
    items = _categories(_build(facts=_facts(core_concerns="fuel_efficiency"), notes=notes))["核心关注"]
    assert items[0]["reasoning_detail"]["summary"] == "客户反复问油耗"
    assert items[0]["reasoning_detail"]["confidence"] == 0.9


# ---------------------------------------------------------------- 断言账本兜底（2026-10-29）

def _claims(*entries) -> str:
    return json.dumps({"fields": list(entries)}, ensure_ascii=False)


def test_card_uses_claim_reasoning_chain_when_notes_have_no_entry():
    """Notes 里没有该字段的条目时，卡片从**断言账本**取推导链，而不是退回审计话。

    心理画像的三项来自 ``personality_and_preferences`` / ``communication_rhythm`` /
    ``emotion``，这三个字段不在 Notes 契约内，Notes 永远不会有它们的条目。改造前
    卡片对它们只能写「由上游已定稿的事实直接给出，未作二次推断」——销售读不懂。
    它们的推导链一直存在于 L1 的 ``reasoning_chain`` 里，``claims_json`` 就是通道。
    """
    payload = _build(
        facts=_facts(personality_and_preferences="rational"),
        claims=_claims(
            {
                "attribute": "personality_and_preferences",
                "value": "rational",
                "basis": "inferred_low",
                "basis_of_inference": ["客户关注智驾付费方式、维修保养成本"],
                "reasoning_chain": ["客户关注参数、成本与技术细节", "决策偏理性", "判断为理性型"],
                "confidence": 0.55,
            }
        ),
    )
    detail = payload["analysis_result"]["decision_psychology"][0]["reasoning_detail"]
    assert detail["steps"] == ["客户关注参数、成本与技术细节", "决策偏理性", "判断为理性型"]
    assert detail["evidence"] == ["客户关注智驾付费方式、维修保养成本"]
    assert "未作二次推断" not in detail["steps"][0]
    assert 0.0 < detail["confidence"] <= 1.0


def test_notes_wording_still_wins_over_the_claim_ledger():
    """Notes 有条目时仍以 Notes 为准——账本只是退路，不是新的首选。

    这条守住 ``_FactView`` 的原始设计：同一事实在 Notes 与卡片上必须是同一种说法。
    """
    notes = _notes(
        {
            "attribute_key": "core_concerns",
            "attribute_value": "fuel_efficiency",
            "reasoning_detail": {
                "summary": "客户反复问油耗",
                "steps": ["问了几次油耗"],
                "evidence": ["我比较看重油耗"],
                "confidence": 0.9,
            },
        }
    )
    claims = _claims(
        {
            "attribute": "core_concerns",
            "value": "fuel_efficiency",
            "basis": "inferred_low",
            "reasoning_chain": ["账本里的推导链不该被用到"],
        }
    )
    items = _categories(
        _build(facts=_facts(core_concerns="fuel_efficiency"), notes=notes, claims=claims)
    )["核心关注"]
    assert items[0]["reasoning_detail"]["steps"] == ["问了几次油耗"]


def test_claim_ledger_accepts_both_payload_shapes():
    """``{"fields": [...]}`` 与 ``{"claims": [...]}`` 都要认。

    v2 起 L1 输出 ``fields``，既有批次产物是 ``claims``。只认一种会让离线回放出来的
    卡片与新跑批次长得不一样，而那正是排查「改了没改」时最需要的一致性。
    """
    entry = {
        "attribute": "emotion",
        "value": "guard_against",
        "basis": "inferred_low",
        "reasoning_chain": ["客户回避追问", "推断存在防备心理"],
    }
    for shape in ({"fields": [entry]}, {"claims": [entry]}):
        payload = _build(
            facts=_facts(emotion="guard_against"),
            claims=json.dumps(shape, ensure_ascii=False),
        )
        detail = payload["analysis_result"]["decision_psychology"][0]["reasoning_detail"]
        assert detail["steps"] == ["客户回避追问", "推断存在防备心理"]


def test_malformed_claims_degrade_instead_of_crashing():
    """账本不是 JSON 或条目不成形时，退化成空账本——卡片照常产出。"""
    payload = _build(
        facts=_facts(emotion="guard_against"),
        claims="不是 JSON",
    )
    entries = payload["analysis_result"]["decision_psychology"]
    assert [e["key"] for e in entries] == ["emotion_state"]
    assert set(entries[0]["reasoning_detail"]) == {"summary", "steps", "evidence", "confidence"}


def test_callback_build_node_receives_the_claim_ledger():
    """节点必须把 ``claims_json`` 接进来，否则上面几条修复在真实运行里全都不生效。

    **这条是接线守卫**：``_FactView`` 能读账本，但节点不给它，产线与单测就会出现
    「本地绿、跑批依旧全是审计话」的分裂。
    """
    from customer_profile.workflows import customer_profile_pipeline as pipe

    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    build = nodes[pipe.CALLBACK_BUILD]
    sources = {b.target: tuple(b.source) for b in build.bindings}
    assert sources["claims_json"] == (pipe.L1_INJECT, "claims_json")


# ---------------------------------------------------------------- 对客文案自净（2026-10-29）

INTERNAL_PHRASES = (
    "已定稿事实",
    "未作二次推断",
    "据现有记录归纳",
    "由表达层",
    "由商机定级环节",
    "检视已定稿",
    "上游已定稿",
)
"""回写体里**不得出现**的管线口径（销售读不懂，且会暴露内部结构）。

这份清单是「对客产出必须真人可读」这条口径的可执行版本。新增兜底分支时若又把
内部说法写进去，这条用例会直接打红——比人工读产物可靠。
"""


def _all_details(payload) -> list[dict]:
    out: list[dict] = []
    ar = payload["analysis_result"]
    for entry in ar.get("basic_notes_updates") or []:
        out.append(entry["reasoning_detail"])
    for item in (ar.get("ai_insight") or {}).values():
        out.append(item["reasoning_detail"])
    for block in ar.get("purchase_decisions") or []:
        out.extend(i["reasoning_detail"] for i in block["items"])
    for key in ("win_conditions", "loss_conditions"):
        out.extend(i["reasoning_detail"] for i in (ar.get("deal_battle") or {}).get(key) or [])
    out.extend(e["reasoning_detail"] for e in ar.get("decision_psychology") or [])
    return out


def test_no_pipeline_vocabulary_reaches_the_sales_rep():
    """四个区块的每一段文案都不出现管线口径。

    覆盖两类来源：代码拼的兜底串（填充项、表达层 supplement）与**模型写的**句子
    （表达层实测会写「已定稿事实显示客户处于买车中」）。后者改不到源头，只能出口自净。
    """
    express = _express(
        profile_summary={
            "personality_summary": "已定稿事实显示客户处于买车中，核心关注点为安全",
            "overall_tag": "务实中产",
        },
        resistances=[
            {"resistance_name": "价格不透明"},  # 无 reasoning_detail → 走代码兜底
            {"resistance_name": "交付周期长", "description": "担心等太久"},
        ],
        emotion_state={"current_state": {"value": "谨慎观望"}},
    )
    payload = _build(
        facts=_facts(
            purchase_type="personal",  # 触发「暂无换车动因」填充项
            deal_level="C",            # 走定级兜底（无 deal_level_detail）
            emotion="guard_against",
            personality_and_preferences="rational",
        ),
        express=express,
    )
    for detail in _all_details(payload):
        blob = " ".join(
            [detail["summary"], *detail["steps"], *detail["evidence"]]
        )
        for phrase in INTERNAL_PHRASES:
            assert phrase not in blob, f"回写体出现管线口径「{phrase}」：{blob[:120]}"


def test_filler_item_explains_absence_in_sales_language():
    """空态填充项说的是「资料里没有」，不是「已定稿事实中未见」。

    「已定稿事实」是本管线的内部说法；销售看不懂它指什么，也就无从判断这条是
    「没查到」还是「查到了但没用」。填充项本就无证据可引，``steps`` 至少要给一句
    读得懂的说明，而不是一句审计话。
    """
    items = _categories(_build(facts=_facts(purchase_type="personal")))
    change = items["换车动因"][0]
    assert "客户资料" in change["reasoning_detail"]["summary"]
    assert "已定稿" not in change["reasoning_detail"]["summary"]
    assert "已定稿" not in change["reasoning_detail"]["steps"][0]
    assert change["reasoning_detail"]["evidence"] == []


def test_last_resort_detail_speaks_about_customer_material():
    """Notes 与断言账本都没有时，末级兜底也只能说「客户资料里已有这条信息」。

    用 ``old_car_model`` 而非 ``purchase_purpose``：后者在调用处传了自己的 ``summary``
    （「购车用途为家用」），走不到末级分支。
    """
    payload = _build(facts=_facts(old_car_model="2019款别克君威"))
    detail = _categories(payload)["当前用车"][0]["reasoning_detail"]
    assert "客户资料" in detail["summary"]
    for phrase in INTERNAL_PHRASES:
        assert phrase not in detail["summary"]
        assert all(phrase not in s for s in detail["steps"])


# ---------------------------------------------------------------- 唯一性

def test_duplicate_item_ids_raise_instead_of_silently_merging():
    """同客户内 id 重复必须**抛错**，不得静默去重。

    静默去重会把两条不同的结论合成一条，卡片上少一项而无人发现；抛错至少让这次回传
    失败并在日志里留痕。宁可失败，也不要一份悄悄缺项的画像。
    """
    with pytest.raises(ValueError, match="重复 id"):
        cb._assert_unique_ids(["focus-a", "focus-a"], "测试")


def test_win_and_loss_share_one_id_space():
    """``win_conditions`` 与 ``loss_conditions`` 的 id 也需全局唯一（契约 §2.5 明文）。"""
    battle = _build(facts=_facts(core_concerns="油耗", brand_concerns="保值率"))[
        "analysis_result"
    ]["deal_battle"]
    ids = [i["id"] for i in battle.get("win_conditions", [])] + [
        i["id"] for i in battle.get("loss_conditions", [])
    ]
    assert len(ids) == len(set(ids))


def test_ids_are_stable_across_runs():
    """id 由内容摘要决定，同输入恒同 id。

    ``hash()`` 对 str 按进程随机加盐——那会让同一客户每次回传的 id 都变，
    而回写是「先删后建」，服务端侧的引用（人工锁定、前端深链）会全部失配。
    """
    facts = _facts(core_concerns="油耗", purchase_nature="replacement", pain_points="车太小")
    first = _build(facts=facts)["analysis_result"]["purchase_decisions"]
    second = _build(facts=facts)["analysis_result"]["purchase_decisions"]
    assert first == second


# ---------------------------------------------------------------- 契约一致性

def test_callback_keys_are_a_subset_of_the_contract():
    """回传白名单即契约字段集——两个常量不得漂移。"""
    assert set(inf.CALLBACK_KEYS) == set(nc.ALL_KEYS)
    assert not (set(inf.INTERNAL_ONLY_KEYS) & set(inf.CALLBACK_KEYS))


def test_tolerates_unparsable_inputs():
    """上游给的不是 JSON 时退化成空产物，而不是抛错中断整条链路。"""
    payload = json.loads(cb.build_analysis_result("1", "b", "不是 JSON", "也不是", "还是不是"))
    assert payload["analysis_result"]["profile_summary"] == {}
    assert payload["analysis_result"]["basic_notes_updates"] == []


# ---------------------------------------------------------------- 图接线（业务测试臂）

def test_callback_nodes_exist_in_the_business_test_arm():
    """回传旁支的两个节点必须在业务测试臂的图里，且接在 L3 之后。"""
    from customer_profile.workflows import customer_profile_pipeline as pipe

    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    assert pipe.CALLBACK_BUILD in nodes and pipe.CALLBACK_WRITE in nodes

    build = nodes[pipe.CALLBACK_BUILD]
    assert build.node_type == "code"
    assert build.predecessors == (pipe.PRODUCTION,)
    assert build.outputs == ("callback_json",)
    # 指向 ``main`` 而不是 ``build_analysis_result``：后者返回 JSON 字符串，
    # 而 execute_code 要求 code 函数返回字段字典。
    assert build.config["function"] == "customer_profile.callback_build:main"

    write = nodes[pipe.CALLBACK_WRITE]
    assert write.node_type == "http-request"
    assert write.predecessors == (pipe.CALLBACK_BUILD,)


def test_writeback_node_is_gated_by_the_persistent_switch():
    """回写节点带 ``@writeback``，且请求体整段取自 ``callback_json``。

    **``@body_ref`` 而不是 ``@body_template``**：后者恒把引用渲染成字符串，产不出
    契约要求的 ``analysis_result`` 对象。两条断言一起钉，因为「换成模板」这种改法
    会让回传体形状静默变回字符串，而本地看不出任何异常。
    """
    from customer_profile.workflows import customer_profile_pipeline as pipe

    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    write = nodes[pipe.CALLBACK_WRITE]
    assert write.config.get("@writeback") is True
    assert write.config.get("@body_ref") is True
    assert "@body_template" not in write.config
    assert write.config["@path"] == f"{pipe.PROFILE_API_PREFIX}callback/update-profile"

    sources = {b.target: tuple(b.source) for b in write.bindings}
    assert sources["@body_ref"] == (pipe.CALLBACK_BUILD, "callback_json")


def test_end_reads_result1_from_the_callback_payload():
    """``END`` 挂 ``CALLBACK_BUILD``：运行台「结果」页展示的 ``result1`` 是 v2 回传体。

    挂 ``PRODUCTION`` 时展示的是 L3 九板块旧结构，与平台实际画像不是同一份数据
    （实测核对时拿错产物）。L3 的旧结构仍留在 ``PRODUCTION`` 节点出参里备查。
    """
    from customer_profile.workflows import customer_profile_pipeline as pipe

    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    end = nodes[pipe.END]
    assert end.predecessors == (pipe.CALLBACK_BUILD,)
    assert {b.target for b in end.bindings} == {"result1"}
    assert [b.source for b in end.bindings] == [(pipe.CALLBACK_BUILD, "callback_json")]
    assert pipe.WORKFLOW.outputs == {"result1": "result1"}
    assert pipe.WORKFLOW.exits == (pipe.END,)


def test_display_name_is_the_business_test_arm_but_workflow_id_is_frozen():
    """对外口径改「业务测试臂」，但 ``workflow_id`` 不得改动。

    ``workflow_id`` 一动，``scripts/run_baseline.py --workflow-id`` 与既有批次
    （``pipeline-v3.3-39``）的可比性判据一并断掉。
    """
    from customer_profile.workflows import customer_profile_pipeline as pipe

    assert pipe.DISPLAY_NAME == "业务测试臂"
    assert pipe.WORKFLOW.display_name == "业务测试臂"
    assert pipe.WORKFLOW_ID == "customer_profile_pipeline"
    assert pipe.WORKFLOW.workflow_id == "customer_profile_pipeline"

"""L3 校验与表达层（代码校验 + 同源装配）的语义回归。

装置口径与 `tests/conftest.py` 一致：**不出网、不依赖批次产物、干净检出上全绿**。
批次端到端的离线回放见 `scripts/probe_pipeline_l2.py`；本文件用合成夹具钉住判定语义。

为什么这几条要单独钉住：L3 替代的是 production 的 12 个 LLM 节点，它的两处代码判定
**错了不报错、只会让产物悄悄变样**——

- `validate_l3` 放过一条编造的确定值，销售就会把模型猜的当成客户说的；
- `assemble_l3` 装配形状与基线不同形，两臂产物立刻不可比（AB 的可比性判据）。

因此每条判据都要有正面用例，也要有「不该拦的没拦」的反例。
"""

from __future__ import annotations

import json

from customer_profile import projection as proj
from customer_profile.workflows import customer_profile_l3 as l3
from customer_profile.workflows import customer_profile_pipeline as pipe


# ---------------------------------------------------------------- 夹具

def claims_json(*claims) -> str:
    return json.dumps({"fields": list(claims)}, ensure_ascii=False)


def claim(attribute, value, *, basis="fact", channel="外呼录音信息", quote=None,
          subject="customer", **kwargs):
    entry = {
        "attribute": attribute,
        "value": value,
        "basis": basis,
        "subject": subject,
        "record_channel": channel,
    }
    if quote is not None:
        entry["quote"] = quote
    entry.update(kwargs)
    return entry


def notes_json(*entries) -> str:
    return json.dumps(list(entries), ensure_ascii=False)


def facts_json(**pairs) -> str:
    return json.dumps(pairs, ensure_ascii=False)


# ---------------------------------------------------------------- 判据一：无依据确定值率

def test_fact_with_anchor_passes():
    """有 quote 的 fact 是「可回溯的确定值」，不得报错。"""
    text = claims_json(claim("occupation", "教师", quote="我是老师"))
    out = l3.validate_l3(text, facts_json(occupation="教师"), notes_json())
    assert out["valid"] is True and out["unbacked_count"] == 0


def test_fact_without_anchor_is_blocked():
    """fact 指不出原文、也不是登记档 → error。这是「编造的确定性」的入口。"""
    text = claims_json(claim("occupation", "教师", quote=None))
    out = l3.validate_l3(text, facts_json(occupation="教师"), notes_json())
    assert out["valid"] is False and out["unbacked_count"] == 1
    assert [i["rule"] for i in json.loads(out["issue_json"])] == ["unbacked_fact"]


def test_registry_channel_needs_no_anchor():
    """登记档由系统直接给出，本就不需要原文锚点（设计稿 §5.4）。"""
    text = claims_json(
        claim("company", "某某电力局", channel="销售人员人工确认的客户Notes信息", quote=None)
    )
    out = l3.validate_l3(text, facts_json(company="某某电力局"), notes_json())
    assert out["valid"] is True and out["unbacked_count"] == 0


def test_inferred_claims_are_not_counted_as_certainties():
    """只有 ``fact`` 进「确定值」的分子；推断类不参与该率。"""
    text = claims_json(
        claim("occupation", "教师", basis="inferred_low", channel="", quote=None),
        claim("age", "40-49岁", basis="inferred_high", channel="", quote=None),
    )
    out = l3.validate_l3(text, facts_json(occupation="教师", age="40-49岁"), notes_json())
    assert out["valid"] is True and out["fact_count"] == 0


# ---------------------------------------------------------------- 判据二：枚举合法

def test_legal_enum_passes():
    text = claims_json(claim("core_concerns", "fuel_efficiency", quote="想省油"))
    out = l3.validate_l3(text, json.dumps({"core_concerns": ["fuel_efficiency"]}), notes_json())
    assert out["valid"] is True


def test_illegal_enum_is_blocked_when_not_downgraded():
    """未被降级的非法枚举 → error（写成确定值却不是契约键）。"""
    text = claims_json()
    out = l3.validate_l3(text, facts_json(purchase_type="想买但还没定"), notes_json())
    assert out["valid"] is False
    assert any(i["rule"] == "illegal_enum" for i in json.loads(out["issue_json"]))


def test_downgraded_illegal_enum_is_warning_not_error():
    """P3 规则 d 保留的客户原话（已降 inferred_low）**不得**阻塞表达。

    这是 P3 与 P4 的接口：规则 d 说「留原值 + 降级 + 转确认」，L3 校验若照旧拦，
    等于把降级策略又废掉一遍。降级的记 warning——仍可见、仍待人工归一键。
    """
    text = claims_json(
        claim("viewing_progress", "在看", basis="inferred_low", channel="", quote=None)
    )
    basis = json.dumps({"viewing_progress": "inferred_low"})
    out = l3.validate_l3(text, facts_json(viewing_progress="在看"), notes_json(), basis)
    assert out["valid"] is True
    rules = {i["rule"]: i["severity"] for i in json.loads(out["issue_json"])}
    assert rules == {"illegal_enum_downgraded": "warning"}


def test_multi_value_enum_checks_each_item():
    """多选字段逐项校验，一项不合法也要被看见。"""
    out = l3.validate_l3(
        claims_json(), json.dumps({"core_concerns": ["fuel_efficiency", "想省油"]}), notes_json()
    )
    detail = json.loads(out["issue_json"])[0]["detail"]
    assert detail == [{"attribute": "core_concerns", "value": "想省油"}]


def test_structured_enum_with_key_is_legal():
    out = l3.validate_l3(
        claims_json(),
        json.dumps({"family_structure": {"key": "has_children", "input_value": "2"}}),
        notes_json(),
    )
    assert out["valid"] is True


# ---------------------------------------------------------------- 判据三：Notes 同源

def test_notes_field_absent_from_facts_is_flagged():
    """Notes 是 facts 的投影。出现 facts 里没有的键，说明有人绕过 L2 改了 Notes。"""
    out = l3.validate_l3(
        claims_json(),
        facts_json(occupation="教师"),
        notes_json({"attribute_key": "annual_income", "attribute_value": "30万"}),
    )
    assert out["valid"] is False
    assert any(i["rule"] == "notes_not_from_facts" for i in json.loads(out["issue_json"]))


def test_notes_field_present_in_facts_is_clean():
    out = l3.validate_l3(
        claims_json(),
        facts_json(occupation="教师"),
        notes_json({"attribute_key": "occupation", "attribute_value": "教师"}),
    )
    assert out["valid"] is True and out["issue_count"] == 0


def test_empty_inputs_are_valid_not_an_error():
    """空记录集是合法情形（分诊层 empty 档），不是校验失败。"""
    out = l3.validate_l3("[]", "{}", "[]")
    assert out["valid"] is True and out["issue_count"] == 0


def test_unparsable_inputs_degrade_gracefully():
    out = l3.validate_l3("不是 JSON", "也不是", "还是不是")
    assert out["valid"] is True


# ---------------------------------------------------------------- 装配：9 节同形

def test_assemble_emits_all_nine_sections_even_when_expression_is_empty():
    """表达层什么都没给时，9 个 section 仍以空形态在位——缺就是缺，**不编造**。

    ``section_count`` 统计的是**实到节数**（补空之前），所以这里是 0 而不是 9；
    9 个键仍然都在位的这件事，由 key set 断言负责。混为一谈会把「缺节」这个
    必须可见的事件掩盖成「一切正常」。
    """
    out = l3.assemble_l3("13500000000", "b1", facts_json(occupation="教师"), notes_json(), "{}")
    sections = json.loads(out["result1"])["analysis_result"]
    assert out["section_count"] == 0
    assert json.loads(out["sections_missing"]) == list(l3.SECTION_KEYS)
    for key in l3.SECTION_KEYS:
        assert key in sections
    assert sections["inferred_tags"] == []
    assert sections["profile_summary"] == {}
    assert sections["emotion_state"] == {}


def test_assemble_reports_arrived_section_count_not_padded_count():
    """实到节数必须少于 9 时能被看见——补空之后每个键都在，晚统计等于没统计。"""
    expr = json.dumps({"analysis_result": {
        "profile_summary": {"gender": "男"},
        "inferred_tags": [{"id": "tag_001", "title": "务实派"}],
    }}, ensure_ascii=False)
    out = l3.assemble_l3("1", "", "{}", notes_json(), expr)
    assert out["section_count"] == 2
    missing = json.loads(out["sections_missing"])
    assert "purchase_motivations" in missing and "emotion_state" in missing
    assert "profile_summary" not in missing


def test_assemble_hoists_sections_nested_inside_another_section():
    """表达层把 6/7/8/9 节写进 ``customer_overview`` 内部时，装配必须上提归位。

    这是 v3.1 实测踩到的坑：提示词里第 6 节的标题与 7、8 挤在同一行，紧跟第 5 节
    （对象型）之后，模型据此把这几节当成第 5 节的子键。原先的装配只抄顶层键，
    嵌套的值被补空静默抹平——内容还在，位置错了，卡片上就成了空白。
    """
    expr = json.dumps({"analysis_result": {
        "profile_summary": {"gender": "男"},
        "inferred_tags": [{"id": "tag_001", "title": "务实派"}],
        "usage_scenarios": [{"id": "scn_001", "title": "通勤"}],
        "communication_strategies": [{"strategy_name": "政策驱动策略"}],
        "customer_overview": {
            "business_opp_level": {"value": "A"},
            "purchase_motivations": [{"motivation_name": "换购省钱"}],
            "product_preferences": [{"preference_name": "长续航"}],
            "resistances": [{"resistance_name": "价格不透明", "severity": "高"}],
            "emotion_state": {"current_state": {"value": "谨慎观望"}},
        },
    }}, ensure_ascii=False)
    out = l3.assemble_l3("1", "", "{}", notes_json(), expr)
    sections = json.loads(out["result1"])["analysis_result"]

    assert sections["purchase_motivations"] == [{"motivation_name": "换购省钱"}]
    assert sections["product_preferences"] == [{"preference_name": "长续航"}]
    assert sections["resistances"][0]["severity"] == "高"
    assert sections["emotion_state"]["current_state"]["value"] == "谨慎观望"
    # 上提后不得在原处留副本
    assert "purchase_motivations" not in sections["customer_overview"]
    assert "emotion_state" not in sections["customer_overview"]
    # 第 5 节自己的子键不受影响
    assert sections["customer_overview"]["business_opp_level"] == {"value": "A"}
    # 归位算恢复，不算缺节
    assert out["section_count"] == 9
    assert json.loads(out["sections_missing"]) == []
    assert sorted(json.loads(out["sections_hoisted"])) == [
        "emotion_state", "product_preferences", "purchase_motivations", "resistances",
    ]


def test_assemble_prefers_top_level_value_over_nested_one():
    """顶层已有该节时，嵌套的那份是多余的，不得覆盖顶层。"""
    expr = json.dumps({"analysis_result": {
        "purchase_motivations": [{"motivation_name": "顶层"}],
        "customer_overview": {"purchase_motivations": [{"motivation_name": "嵌套"}]},
    }}, ensure_ascii=False)
    out = l3.assemble_l3("1", "", "{}", notes_json(), expr)
    sections = json.loads(out["result1"])["analysis_result"]
    assert sections["purchase_motivations"] == [{"motivation_name": "顶层"}]
    assert "purchase_motivations" not in sections["customer_overview"]


def test_assemble_shape_matches_baseline():
    """产物形状与基线同形：顶层 phone / batch_id / analysis_result。"""
    out = l3.assemble_l3("13500000000", "b1", "{}", notes_json(), "{}")
    payload = json.loads(out["result1"])
    assert set(payload) == {"phone", "batch_id", "analysis_result"}
    assert set(payload["analysis_result"]) == set(l3.SECTION_KEYS) | {"basic_notes_updates"}


def test_assemble_puts_notes_verbatim_into_analysis_result():
    """Notes 由代码装配（取自 L2），不经表达层转手——同源由此保证。"""
    notes = notes_json(
        {"attribute_key": "occupation", "attribute_value": "教师",
         "reasoning_detail": {"summary": "s", "steps": [], "evidence": [], "confidence": 0.9}}
    )
    out = l3.assemble_l3("1", "", "{}", notes, "{}")
    merged = json.loads(out["result1"])["analysis_result"]["basic_notes_updates"]
    assert merged == json.loads(notes)


def test_assemble_keeps_expression_sections():
    """表达层给出的 section 原样保留（它只做组织与措辞）。"""
    expr = json.dumps({"analysis_result": {
        "profile_summary": {"age": "40 ~ 49"},
        "inferred_tags": [{"id": "tag_001", "title": "精算务实派"}],
    }}, ensure_ascii=False)
    out = l3.assemble_l3("1", "", "{}", notes_json(), expr)
    sections = json.loads(out["result1"])["analysis_result"]
    assert sections["profile_summary"] == {"age": "40 ~ 49"}
    assert sections["inferred_tags"][0]["title"] == "精算务实派"


def test_assemble_accepts_bare_sections_without_wrapper():
    """表达层直接给 9 个键（没包 analysis_result）也要认。"""
    expr = json.dumps({"profile_summary": {"gender": "男"}}, ensure_ascii=False)
    out = l3.assemble_l3("1", "", "{}", notes_json(), expr)
    assert json.loads(out["result1"])["analysis_result"]["profile_summary"] == {"gender": "男"}


# ---------------------------------------------------------------- 图定义：预置资产与接线

def test_l3_graph_shape():
    """1 个 LLM 节点（表达），其余全是代码与预置资产。

    节点账：start + end 各 1、预置资产 6、代码节点 3（校验 / 装配）与 LLM 1。
    """
    llm = [n for n in l3.WORKFLOW.nodes if n.node_type == "llm"]
    assert [n.node_id for n in llm] == [l3.EXPRESS]
    assert len(l3.WORKFLOW.nodes) == len(l3.PRESET_ASSETS) + 5
    kinds = {n.node_type for n in l3.WORKFLOW.nodes}
    assert kinds == {"start", "end", "code", "llm", "template-transform"}


def test_evidence_format_asset_is_not_bound_to_the_expression_node():
    """「证据线索数据格式」预置留在图里但**不喂给表达层**。

    那份格式是给「整理证据线索」用的，v2 里那一步已由 L1 承担；把它塞进表达层只会
    诱导模型按证据格式输出、而不是按画像格式。留节点是为了 6 个资产的清单完整、
    便于日后复用，不接进本次表达。
    """
    node = next(n for n in l3.WORKFLOW.nodes if n.node_id == l3.EXPRESS)
    sources = {b.source[0] for b in node.bindings}
    assert l3.EVIDENCE_FORMAT not in sources


def test_preset_assets_reference_production_templates_by_original_name():
    """6 个业务资产按**原名**引用，不复制副本——「一字不改」由此可验证。

    若有人把它们复制到本模块的 slug 下，这里会红：正文就有了第二份，日后必然漂移。
    """
    nodes = {n.node_id: n for n in l3.WORKFLOW.nodes}
    for node_id, _title in l3.PRESET_ASSETS:
        node = nodes[node_id]
        assert node.node_type == "template-transform"
        assert node.config["template"] == f"customer_profile_production__{node_id}"
        assert node.config["template"].startswith("customer_profile_production__")


def test_expression_node_binds_only_facts_and_assets():
    """表达层的输入只有「已定稿事实 + 业务资产」，**没有证据原文**。

    这是「表达不得新增事实」的结构保证：它拿不到原文，就无处编造。
    """
    node = next(n for n in l3.WORKFLOW.nodes if n.node_id == l3.EXPRESS)
    sources = {b.source[0] for b in node.bindings}
    asset_ids = {node_id for node_id, _ in l3.PRESET_ASSETS}
    assert sources <= asset_ids | {l3.VALIDATE}, f"混入了资产与事实之外的来源：{sources - asset_ids - {l3.VALIDATE}}"
    assert l3.VALIDATE in sources, "必须吃到已定稿事实"
    assert len(sources & asset_ids) >= 4, "业务资产要真的接进去，否则表达层无从选词"
    # 结构保证本身：原始证据的入口（L1 三层）不得出现在这里
    assert pipe.L1_INPUT not in sources and pipe.L1_FACTS not in sources
    assert pipe.L1_PERSONA not in sources and pipe.L1_FIELDS not in sources


def test_inferred_low_constant_matches_projection():
    """本模块不导入 projection，靠这条用例钉住两者同值。"""
    assert l3.INFERRED_LOW == proj.INFERRED_LOW


# ---------------------------------------------------------------- 管线接线：成文段已换 L3

def test_pipeline_tail_points_at_l3():
    node = pipe.WORKFLOW.node_map[pipe.PRODUCTION]
    assert node.node_type == "tool"
    assert node.config["@workflow"] == l3.WORKFLOW_ID


def test_pipeline_tail_inputs_match_l3_start_variables():
    """父图给 L3 的每个入参，都必须是 L3 的 start 声明的变量。

    漏传一个不会报错——子流程照样跑，只是拿着空值产出无关结果。这正是要钉的静默故障。
    """
    node = pipe.WORKFLOW.node_map[pipe.PRODUCTION]
    mapping = node.config["@inputs"]
    start = next(n for n in l3.WORKFLOW.nodes if n.node_type == "start")
    declared = set(start.config["variables"])
    assert set(mapping) <= declared, f"L3 未声明：{set(mapping) - declared}"
    # 反向：父侧每个映射值都要真有绑定，否则传的是空
    bound = {b.target for b in node.bindings}
    for child_param, source in mapping.items():
        if not str(source).startswith("@literal:"):
            assert source in bound, f"{child_param} 的父侧入参 {source} 没有绑定"


def test_pipeline_no_longer_carries_the_old_evidence_path():
    """P5 删旧的判据：旧证据整理与兼容覆写都不在图里了。"""
    ids = {n.node_id for n in pipe.WORKFLOW.nodes}
    for gone in ("pipeline_evidence_input", "pipeline_evidence_llm",
                 "pipeline_profile", "pipeline_standard_aggregate",
                 "pipeline_notes_overwrite"):
        assert gone not in ids


def test_pipeline_llm_nodes_are_l1_three_layers_and_l2_fallback():
    """管线里 L1 是三层（事实 / 人设 / 分析投影）加 L2 兜底；成文段交给 L3 子流程。"""
    llm = sorted(n.node_id for n in pipe.WORKFLOW.nodes if n.node_type == "llm")
    assert llm == [pipe.L1_FACTS, pipe.L1_FIELDS, pipe.L1_PERSONA, pipe.L2_FALLBACK]


def test_pipeline_end_reads_result1_from_l3():
    end = pipe.WORKFLOW.node_map[pipe.END]
    assert end.predecessors == (pipe.PRODUCTION,)
    assert [b.source for b in end.bindings] == [(pipe.PRODUCTION, "result1")]

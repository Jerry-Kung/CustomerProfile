"""gate 簇：审查层从「能判」到「能拦」。

这一层的特点是**判据早就有了、动作没有**。旧 ``review_notes`` 判出违规只记一条
``issue``，条目照样进最终回传体——销售看到的仍是带占位串、无依据确定值的画像。

因此这里的用例全部断言**产物本身变了**，而不是「计数增加了」：

- 占位串：条目从输出里消失；
- 非法枚举 / 无锚点 fact：条目的置信度降为 0.5，取值保留；
- 证据位自指：那条证据被摘掉，条目与取值不动。

另有一组「不该拦的没被拦」的反例——一个误杀率高的守卫等于没有守卫。
"""

from __future__ import annotations

import json

from customer_profile import internal_fields as inf
from customer_profile import projection as proj
from customer_profile.rules import NotesState, apply
from customer_profile.rules.clusters import expression as expression_rules
from customer_profile.rules.clusters import hard as hard_rules
from customer_profile.workflows import customer_profile_l3 as l3
from customer_profile.workflows import customer_profile_pipeline as pipe


def note(key, value, *, evidence=None, channel=""):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "record_channel": channel,
        "reasoning_detail": {
            "summary": "",
            "steps": [],
            "evidence": evidence or [],
            "confidence": 0.9,
        },
    }


def gate_actions(entries, basis=None):
    """按 gate 层的真实入参调 HARD 簇，返回动作（不执行）。"""
    return hard_rules.check_notes(
        entries,
        basis=dict(basis or {}),
        is_conclusion=proj._is_conclusion,
        enum_known=inf.is_enum,
        legal_keys=inf.legal_keys,
        display_to_key=inf.display_to_key,
    )


# ---------------------------------------------------------------- 判据函数

def test_placeholder_produces_reject_action():
    actions = gate_actions([note("hobbies", "未提供")])
    assert [a.rule for a in actions] == ["HARD-01"]
    assert actions[0].target == "note"


def test_placeholder_does_not_produce_other_actions():
    """占位串整条跳过后续判据——一条「未提供」没有校验取值的意义。"""
    actions = gate_actions([note("family_structure", "未提供")])
    assert [a.rule for a in actions] == ["HARD-01"]


def test_customer_name_unknown_is_not_a_placeholder():
    """姓名查不到时固定输出「未知」，那是业务结论，不是占位串。"""
    actions = gate_actions([note("customer_name", "未知")])
    assert actions == []


def test_illegal_enum_produces_downgrade_action():
    actions = gate_actions([note("family_structure", "说不清")])
    assert [a.rule for a in actions] == ["HARD-02"]
    assert actions[0].action == "downgrade"


def test_illegal_enum_already_downgraded_is_left_alone():
    """P3 规则 d 保留的客户原话（已降级）不再重复拦。"""
    actions = gate_actions(
        [note("family_structure", "说不清")], basis={"family_structure": "inferred_low"}
    )
    assert actions == []


def test_chinese_display_name_is_not_illegal():
    """展示名能反查回合法键的，不算非法取值（``已婚无孩`` ↔ ``married_no_child``）。"""
    actions = gate_actions([note("family_structure", "已婚无孩")])
    assert actions == []


def test_unbacked_fact_produces_downgrade():
    actions = gate_actions(
        [note("occupation", "教师", evidence=["从对话内容可以判断在事业单位工作"])],
        basis={"occupation": "fact"},
    )
    assert [a.rule for a in actions] == ["HARD-03"]


def test_fact_with_quote_is_clean():
    actions = gate_actions(
        [note("occupation", "教师", evidence=["外呼录音信息：我在学校当老师"])],
        basis={"occupation": "fact"},
    )
    assert actions == []


def test_registry_channel_needs_no_quote():
    actions = gate_actions(
        [note("company", "某某电力局", channel="销售人员人工确认的客户Notes信息")],
        basis={"company": "fact"},
    )
    assert actions == []


def test_inferred_notes_are_not_checked_for_anchor():
    """只有 ``fact`` 才要求指得出原文；推断类没有单句锚点是正常的。"""
    actions = gate_actions(
        [note("age", "40-49岁", evidence=["从工作年限推断"])],
        basis={"age": "inferred_high"},
    )
    assert actions == []


def test_evidence_self_reference_produces_evidence_action():
    actions = expression_rules.check_evidence(
        [note("age", "40-49岁", evidence=["推断为40-49岁"])],
        is_conclusion=proj._is_conclusion,
    )
    assert [a.rule for a in actions] == ["EXPR-02"]
    assert actions[0].target == "evidence"
    assert actions[0].payload["snippet"] == "推断为40-49岁"


# ---------------------------------------------------------------- 执行：产物真的变了

def test_placeholder_entry_is_removed_from_output():
    state = NotesState(notes=[note("hobbies", "未提供"), note("occupation", "教师")])
    actions = gate_actions(state.notes)
    apply(state, actions, max_rounds=1)
    assert [entry["attribute_key"] for entry in state.notes] == ["occupation"]


def test_downgrade_lowers_confidence_but_keeps_value():
    """降级必须**可见**：只改 basis 不改 confidence，销售点开看到的仍是高置信。"""
    state = NotesState(
        notes=[note("family_structure", "说不清")], basis={"family_structure": "fact"}
    )
    apply(state, gate_actions(state.notes, {"family_structure": "fact"}), max_rounds=1)
    assert state.notes[0]["attribute_value"] == "说不清"
    assert state.notes[0]["reasoning_detail"]["confidence"] == 0.5
    assert state.basis["family_structure"] == "inferred_low"


def test_evidence_self_reference_is_stripped_entry_survives():
    state = NotesState(
        notes=[note("age", "40-49岁", evidence=["推断为40-49岁", "外呼录音信息：四十多岁吧"])]
    )
    apply(
        state,
        expression_rules.check_evidence(
            state.notes, is_conclusion=proj._is_conclusion
        ),
        max_rounds=1,
    )
    assert len(state.notes) == 1
    assert state.notes[0]["attribute_value"] == "40-49岁"
    assert state.notes[0]["reasoning_detail"]["evidence"] == ["外呼录音信息：四十多岁吧"]


# ---------------------------------------------------------------- 与节点的接线

def test_review_notes_outputs_post_gate_notes():
    """节点出口必须给**执行后**的 Notes，而不是原样透传。"""
    result = pipe.review_notes(
        json.dumps([note("hobbies", "未提供"), note("occupation", "教师")], ensure_ascii=False),
        "{}",
        "{}",
    )
    kept = json.loads(result["notes_updates_json"])
    assert [entry["attribute_key"] for entry in kept] == ["occupation"]
    assert result["placeholder_removed"] == 1


def test_review_notes_publishes_downgraded_basis():
    """降级结果必须出节点——L3 的 basis 取自审查层。"""
    result = pipe.review_notes(
        json.dumps([note("family_structure", "说不清")], ensure_ascii=False),
        json.dumps({"family_structure": "说不清"}, ensure_ascii=False),
        json.dumps({"family_structure": "fact"}, ensure_ascii=False),
    )
    basis = json.loads(result["basis_json"])
    assert basis["family_structure"] == "inferred_low"
    assert result["enum_illegal"] == 1


def test_review_notes_records_rule_trace():
    result = pipe.review_notes(
        json.dumps([note("hobbies", "未提供")], ensure_ascii=False), "{}", "{}"
    )
    traces = json.loads(result["rule_trace_json"])
    assert [trace["rule_id"] for trace in traces] == ["HARD-01"]
    assert traces[0]["stage"] == "gate"


# ---------------------------------------------------------------- 合并后的判据同源

def test_validate_and_review_share_one_predicate():
    """两处判据必须是同一份实现：Notes 侧与断言侧的输入来源不同，判定相同。"""
    assert l3.hard_rules.is_unbacked_fact is hard_rules.is_unbacked_fact
    assert l3.REGISTRY_CHANNELS is hard_rules.REGISTRY_CHANNELS


def test_validate_l3_still_blocks_unbacked_claims():
    claims = json.dumps(
        [{"attribute": "occupation", "value": "教师", "basis": "fact", "record_channel": ""}],
        ensure_ascii=False,
    )
    out = l3.validate_l3(claims, json.dumps({"occupation": "教师"}), "[]")
    assert out["valid"] is False
    assert out["unbacked_count"] == 1


# ---------------------------------------------------------------- 单条关闭

def test_rules_disabled_removes_gate_rule(monkeypatch):
    """``RULES_DISABLED=HARD-01`` 必须让占位串条目留在产物里——关闭开关要真关得掉。

    这是本次交付的三件事之一（一处关闭）。只接投影层不接 gate 段，会变成
    「配置写了、审查照拦」，排查时无从下手。
    """
    monkeypatch.setattr(pipe, "_disabled_rule_ids", lambda: ("HARD-01",))
    result = pipe.review_notes(
        json.dumps([note("hobbies", "未提供")], ensure_ascii=False), "{}", "{}"
    )
    kept = json.loads(result["notes_updates_json"])
    assert [entry["attribute_key"] for entry in kept] == ["hobbies"]
    assert result["placeholder_removed"] == 0


def test_rules_disabled_is_empty_by_default(monkeypatch):
    """未配置时不得凭空关闭任何规则。"""
    monkeypatch.setattr(pipe, "_disabled_rule_ids", lambda: ())
    result = pipe.review_notes(
        json.dumps([note("hobbies", "未提供")], ensure_ascii=False), "{}", "{}"
    )
    assert json.loads(result["notes_updates_json"]) == []

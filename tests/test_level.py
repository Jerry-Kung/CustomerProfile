"""商机等级定级环节（``level_stage`` / ``build_level_input`` / ``apply_level``）的回归。

**为什么这几条必须有用例**：商机等级是销售排序跟进的第一依据，而它的收口规则
（阶段上限 R04、多维证据门槛 R05、原文定位）全是**静默生效**的——模型越界时不会报错，
只会让一个本该是 B 的客户在卡片上显示成 H。每条规则都要正面钉住，并配一条「该放行的
仍然放行」的反例，免得把规则写成一刀切。

等级取值口径见业务专家《客户商机等级定级规范》，过渡回传映射见 L-008。
"""

from __future__ import annotations

import json

import pytest

from customer_profile import callback_build as cb
from customer_profile.workflows import customer_profile_pipeline as pipe


# ---------------------------------------------------------------- 夹具

def _record(source_name, content, *, status="ok"):
    return {
        "record_id": f"rec_{source_name}",
        "channel": source_name,
        "source_name": source_name,
        "source_class": "declared",
        "status": status,
        "created_at": "2026-10-19T10:00:00",
        "content": content,
        "content_kind": "transcript" if "录音" in source_name else "ocr",
    }


def _records(*records):
    return json.dumps(list(records), ensure_ascii=False)


LEAD_RECORDS = _records(
    _record("企业微信聊天记录", "客户说孩子九月上小学，想换一台大空间的车接送"),
)

OPPORTUNITY_RECORDS = _records(
    _record("企业微信聊天记录", "客户说孩子九月上小学，想换一台大空间的车接送"),
    _record("试驾录音信息", "客户试驾后说底盘挺稳，就是担心后备箱不够放婴儿车"),
)


def _assess(**overrides):
    base = {
        "stage": "opportunity",
        "level": "A",
        "confidence": 0.8,
        "time_window": "14天内",
        "dimensions": {
            "want_to_buy": {"value": "换车接送孩子", "evidence": ["想换一台大空间的车接送"]},
            "can_to_buy": {"value": "预算基本匹配", "evidence": []},
            "when_to_buy": {"value": "九月前要买", "evidence": []},
        },
        "homework_evidence": ["客户试驾后说底盘挺稳"],
        "obstacles": [],
        "steps": ["换车起因是孩子上学", "已试驾并给出正向反馈", "时间窗口明确"],
        "summary": "需求明确、已试驾，预计两周内可推进",
        "next_action": "约家属二次到店并出报价",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


# ---------------------------------------------------------------- 阶段判定

def test_stage_is_lead_without_test_drive_recording():
    """没有试驾录音 → 线索邀约阶段（规范 §2.1）。"""
    result = pipe.level_stage(LEAD_RECORDS)
    assert result["stage"] == pipe.LEVEL_STAGE_LEAD
    assert result["has_test_drive"] is False


def test_stage_is_opportunity_when_test_drive_recording_exists():
    """有实质内容的试驾录音 → 转商机阶段（任意一类试驾数据存在即可）。"""
    result = pipe.level_stage(OPPORTUNITY_RECORDS)
    assert result["stage"] == pipe.LEVEL_STAGE_OPPORTUNITY


def test_empty_test_drive_record_does_not_promote_stage():
    """试驾渠道存在但没有内容，不算「已试驾」——空录音不能把阶段推进。"""
    records = _records(
        _record("试驾录音信息", "", status="ok"),
        _record("企业微信聊天记录", "想换车"),
    )
    assert pipe.level_stage(records)["stage"] == pipe.LEVEL_STAGE_LEAD


def test_customer_saying_they_will_test_drive_does_not_change_stage():
    """客户口头说「准备试驾」不改变阶段（规范 §2.2 明文）。"""
    records = _records(_record("企业微信聊天记录", "我下周准备去试驾一下"))
    assert pipe.level_stage(records)["stage"] == pipe.LEVEL_STAGE_LEAD


# ---------------------------------------------------------------- 输入装配

def test_build_level_input_carries_stage_and_all_records():
    """定级输入必须带阶段、已定稿事实与**全部**记录原文——等级的证据要够厚。"""
    out = pipe.build_level_input(
        OPPORTUNITY_RECORDS,
        facts_json=json.dumps({"planned_purchase_time": "九月前"}, ensure_ascii=False),
    )
    text = out["output"]
    assert out["stage"] == pipe.LEVEL_STAGE_OPPORTUNITY
    assert "转商机阶段" in text
    assert "想换一台大空间的车接送" in text
    assert "客户试驾后说底盘挺稳" in text
    # 字段清单是 L1 的取证任务书，不该混进定级输入
    assert "目标字段清单" not in text


# ---------------------------------------------------------------- 收口：枚举

def test_apply_level_writes_level_detail_and_stage():
    out = pipe.apply_level(
        OPPORTUNITY_RECORDS, "{}", _assess(), stage=pipe.LEVEL_STAGE_OPPORTUNITY
    )
    facts = json.loads(out["facts_json"])
    assert out["parse_ok"] is True
    assert facts["deal_level"] == "A"
    assert facts["business_opp_stage"] == "转商机阶段"
    detail = facts["deal_level_detail"]
    assert set(detail) == {"summary", "steps", "evidence", "confidence"}
    assert detail["steps"][-1].startswith("下一步建议：")
    assert detail["evidence"]


def test_apply_level_accepts_pending_identification():
    """待识别是合法结论，不是非法值——信息不足时不得强行归入 H/A/B/C。"""
    out = pipe.apply_level(
        LEAD_RECORDS, "{}", _assess(level="待识别"), stage=pipe.LEVEL_STAGE_LEAD
    )
    assert json.loads(out["facts_json"])["deal_level"] == "待识别"


def test_apply_level_is_not_overridden_by_the_llm_level():
    """L1 兜底等级是种子，定级环节的结论优先。"""
    facts = json.dumps({"deal_level": "C"}, ensure_ascii=False)
    out = pipe.apply_level(OPPORTUNITY_RECORDS, facts, _assess(level="B"),
                           stage=pipe.LEVEL_STAGE_OPPORTUNITY)
    assert json.loads(out["facts_json"])["deal_level"] == "B"


def test_invalid_level_falls_back_to_l1_level():
    facts = json.dumps({"deal_level": "C"}, ensure_ascii=False)
    out = pipe.apply_level(OPPORTUNITY_RECORDS, facts, _assess(level="很高"),
                           stage=pipe.LEVEL_STAGE_OPPORTUNITY)
    assert out["rule_clamped"] == "invalid_level"
    assert json.loads(out["facts_json"])["deal_level"] == "C"


def test_unparseable_assessment_keeps_existing_facts():
    """定级调用失败时保留 L1 的兜底值，而不是伪装成「客户被评成待识别」。"""
    facts = json.dumps({"deal_level": "B"}, ensure_ascii=False)
    out = pipe.apply_level(OPPORTUNITY_RECORDS, facts, "不是 JSON",
                           stage=pipe.LEVEL_STAGE_OPPORTUNITY)
    assert out["parse_ok"] is False
    assert json.loads(out["facts_json"])["deal_level"] == "B"


# ---------------------------------------------------------------- 收口：规则

def test_lead_stage_caps_h_at_a():
    """线索邀约阶段最高 A（R04）——即使客户意向极强。"""
    assess = _assess(
        level="H",
        stage="lead",
        dimensions={
            "want_to_buy": {"value": "换车接送孩子", "evidence": ["想换一台大空间的车接送"]},
            "can_to_buy": {"value": "预算基本匹配", "evidence": []},
            "when_to_buy": {"value": "九月前要买", "evidence": []},
        },
        homework_evidence=["想换一台大空间的车接送"],
    )
    out = pipe.apply_level(LEAD_RECORDS, "{}", assess, stage=pipe.LEVEL_STAGE_LEAD)
    assert json.loads(out["facts_json"])["deal_level"] == "A"
    assert out["rule_clamped"] == "lead_stage_caps_at_A"


def test_h_without_multidimensional_evidence_downgrades_to_b():
    """H/A 必须同时具备需求、时间、行动证据（R05）；缺行动证据则降为 B。"""
    out = pipe.apply_level(
        OPPORTUNITY_RECORDS,
        "{}",
        _assess(level="H", homework_evidence=[]),
        stage=pipe.LEVEL_STAGE_OPPORTUNITY,
    )
    assert json.loads(out["facts_json"])["deal_level"] == "B"
    assert out["rule_clamped"] == "insufficient_multidimensional_evidence"


def test_h_with_full_evidence_survives():
    """三类证据齐全时 H 应当放行——规则不是一刀切。"""
    out = pipe.apply_level(
        OPPORTUNITY_RECORDS, "{}", _assess(level="H"),
        stage=pipe.LEVEL_STAGE_OPPORTUNITY,
    )
    assert json.loads(out["facts_json"])["deal_level"] == "H"
    assert out["rule_clamped"] == ""


def test_evidence_that_cannot_be_located_is_dropped():
    """证据必须能回定位到原文；定位不到的剔除，不可能出现在卡片上。"""
    out = pipe.apply_level(
        OPPORTUNITY_RECORDS,
        "{}",
        _assess(dimensions={
            "want_to_buy": {"value": "换车", "evidence": ["客户说要把旧车卖了换新的"]},
            "can_to_buy": {"value": "", "evidence": []},
            "when_to_buy": {"value": "", "evidence": []},
        }),
        stage=pipe.LEVEL_STAGE_OPPORTUNITY,
    )
    assert out["evidence_dropped"] == 1
    detail = json.loads(out["facts_json"])["deal_level_detail"]
    assert "客户说要把旧车卖了换新的" not in detail["evidence"]


def test_obstacles_and_next_action_are_written_for_sales():
    """规范 §7.1 要求输出关键障碍与下一步建议，二者进推理链末尾。"""
    out = pipe.apply_level(
        OPPORTUNITY_RECORDS,
        "{}",
        _assess(obstacles=[{"name": "家属未到店", "solvable_in_window": True,
                            "evidence": []}]),
        stage=pipe.LEVEL_STAGE_OPPORTUNITY,
    )
    steps = json.loads(out["facts_json"])["deal_level_detail"]["steps"]
    assert any("关键障碍：家属未到店" in s for s in steps)
    assert any(s.startswith("下一步建议：") for s in steps)


# ---------------------------------------------------------------- 过渡回传映射（L-008）

def test_contract_level_transition_mode_maps_h_and_drops_pending():
    """过渡态：H→A；待识别不回传（不是 C 级）；旧 D→C。"""
    assert cb.contract_level("H", passthrough=False) == "A"
    assert cb.contract_level("待识别", passthrough=False) is None
    assert cb.contract_level("D", passthrough=False) == "C"
    assert cb.contract_level("A", passthrough=False) == "A"


def test_contract_level_passthrough_returns_internal_values():
    """切回模式：五值原样透传，前端整改完成后改配置即可，无需改代码。"""
    for level in ("H", "A", "B", "C", "待识别"):
        assert cb.contract_level(level, passthrough=True) == level


def test_transition_mode_explains_the_h_to_a_display():
    """H 映射为 A 时，推理链末尾要说明展示口径——不能出现「值 A、依据写 H」的矛盾。"""
    facts = json.dumps(
        {
            "deal_level": "H",
            "deal_level_detail": {
                "summary": "条件已齐备",
                "steps": ["已讨论订单与交付"],
                "evidence": ["明天来交定金"],
                "confidence": 0.9,
            },
        },
        ensure_ascii=False,
    )
    payload = json.loads(cb.build_analysis_result("13500000000", "b1", facts))
    item = payload["analysis_result"]["ai_insight"]["purchase_intent_level"]
    assert item["value"] == "A"
    assert any("内部定级为 H" in s for s in item["reasoning_detail"]["steps"])


def test_ai_insight_prefers_the_dedicated_level_detail():
    """等级详情取定级环节的推理链，而不是 Notes 的通用兜底文案。"""
    facts = json.dumps(
        {
            "deal_level": "A",
            "deal_level_detail": {
                "summary": "需求明确且已试驾",
                "steps": ["客户提到孩子九月上小学", "已试驾并给出正向反馈"],
                "evidence": ["想换一台大空间的车接送"],
                "confidence": 0.82,
            },
        },
        ensure_ascii=False,
    )
    payload = json.loads(cb.build_analysis_result("13500000000", "b1", facts))
    detail = payload["analysis_result"]["ai_insight"]["purchase_intent_level"]["reasoning_detail"]
    assert detail["steps"] == ["客户提到孩子九月上小学", "已试驾并给出正向反馈"]
    assert detail["confidence"] == 0.82
    assert detail["evidence"] == ["想换一台大空间的车接送"]


def test_pending_level_is_omitted_in_transition_mode(monkeypatch):
    """待识别在过渡态不回传：前端留空，而不是显示成 C。"""
    from customer_profile import settings

    monkeypatch.setenv("PURCHASE_INTENT_LEVEL_PASSTHROUGH", "false")
    settings.get_settings.cache_clear()
    try:
        facts = json.dumps({"deal_level": "待识别"}, ensure_ascii=False)
        payload = json.loads(cb.build_analysis_result("13500000000", "b1", facts))
        assert "ai_insight" not in payload["analysis_result"]
    finally:
        settings.get_settings.cache_clear()


def test_pending_level_passes_through_when_enabled(monkeypatch):
    from customer_profile import settings

    monkeypatch.setenv("PURCHASE_INTENT_LEVEL_PASSTHROUGH", "true")
    settings.get_settings.cache_clear()
    try:
        facts = json.dumps({"deal_level": "待识别"}, ensure_ascii=False)
        payload = json.loads(cb.build_analysis_result("13500000000", "b1", facts))
        item = payload["analysis_result"]["ai_insight"]["purchase_intent_level"]
        assert item["value"] == "待识别"
    finally:
        settings.get_settings.cache_clear()

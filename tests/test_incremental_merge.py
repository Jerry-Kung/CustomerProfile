"""合并账本（``incremental.merge``）的回归。

三件事：底座断言依赖的渠道消失时**退场**、同字段取舍交给时近性规则、产物形状能直接
喂 ``project_claims``。最后一条最容易被破坏——形状一变，投影层就会静默产出空事实。
"""

from __future__ import annotations

import json

from customer_profile.incremental.merge import merge_claims
from customer_profile.workflows import customer_profile_pipeline as pipe

BASE = "上一版画像"


def _base(attribute, value, *, channel=BASE, basis="inferred_high"):
    return {
        "attribute": attribute,
        "value": value,
        "basis": basis,
        "record_channel": channel,
        "subject": "customer",
        "observed_at": "2026-11-01T10:00:00",
        "confidence": 0.8,
        "basis_of_inference": ["上一版的依据"],
        "reasoning_chain": ["上一版的推导"],
    }


def _new(attribute, value, *, channel="销售跟进记录", basis="inferred_high"):
    return {
        "attribute": attribute,
        "value": value,
        "basis": basis,
        "record_channel": channel,
        "quote": "原文",
    }


def _row(channel="OntologyLeadFollowRecord", key="k1", version=1):
    return {
        "channel": channel,
        "source_key": key,
        "source_version": version,
        "created_at": "2026-11-02T10:00:00",
        "raw_payload": {"data": channel},
    }


def _fields(outcome):
    return json.loads(outcome["claims_json"])["fields"]


def test_new_claim_takes_over_in_a_process_field():
    outcome = merge_claims(
        base_fields={"fields": [_base("budget_range", "18-20万")]},
        new_fields={"fields": [_new("budget_range", "20-25万")]},
        current_rows=[_row()],
    )
    values = [c["value"] for c in _fields(outcome)]
    assert "20-25万" in values
    assert "18-20万" not in values


def test_base_claim_survives_when_no_new_claim():
    outcome = merge_claims(
        base_fields={"fields": [_base("occupation", "教师", channel="销售跟进记录")]},
        new_fields={"fields": []},
        current_rows=[_row()],
    )
    assert [c["value"] for c in _fields(outcome)] == ["教师"]
    assert outcome["base_count"] == 1
    assert outcome["new_count"] == 0


def test_claims_of_a_removed_channel_are_retired():
    outcome = merge_claims(
        base_fields={"fields": [_base("occupation", "教师", channel="销售跟进记录")]},
        new_fields={"fields": []},
        current_rows=[_row("OntologyLeadInfo")],
    )
    assert outcome["retired"] == 1
    assert _fields(outcome) == []
    assert "退场" in json.loads(outcome["retired_json"])[0]["reason"]


def test_claims_without_channel_are_never_retired():
    """手机号注入类断言没有渠道名：不能因为「找不到渠道」被当成已删除。"""
    outcome = merge_claims(
        base_fields={"fields": [_base("phone_number", "13900000001", channel="")]},
        new_fields={"fields": []},
        current_rows=[_row("OntologyLeadInfo")],
    )
    assert outcome["retired"] == 0
    assert _fields(outcome)[0]["value"] == "13900000001"


def test_observed_at_is_filled_from_the_base_update_time():
    outcome = merge_claims(
        base_fields={"fields": [dict(_base("age", "35-40"), observed_at="")]},
        new_fields={"fields": []},
        current_rows=[_row()],
        base_updated_at="2026-11-01T09:00:00",
    )
    assert _fields(outcome)[0]["observed_at"] == "2026-11-01T09:00:00"


def test_output_feeds_project_claims():
    """产物形状必须能直接喂投影层（字段键 → 事实表）。"""
    outcome = merge_claims(
        base_fields={
            "fields": [
                _base("occupation", "教师", channel="销售人员人工确认的客户Notes信息", basis="fact"),
            ]
        },
        new_fields={"fields": []},
        current_rows=[_row()],
    )
    projected = pipe.project_claims(outcome["claims_json"])
    facts = json.loads(projected["facts_json"])
    assert facts["occupation"] == "教师"


def test_multiple_base_claims_on_one_field_all_pass_through():
    """底座同一字段多条（多值/多来源）：本层不做字段级取舍，全部放行交给投影层。"""
    outcome = merge_claims(
        base_fields={
            "fields": [
                _base("hobbies", "钓鱼"),
                _base("hobbies", "露营"),
            ]
        },
        new_fields={"fields": []},
        current_rows=[_row()],
    )
    assert len(_fields(outcome)) == 2


def test_rule_trace_is_recorded_for_rejected_new_claims():
    outcome = merge_claims(
        base_fields={"fields": [_base("age", "35-40", basis="fact")]},
        new_fields={"fields": [_new("age", "30岁左右", channel="朋友圈截图内容信息", basis="inferred_low")]},
        current_rows=[_row()],
    )
    trace = json.loads(outcome["rule_trace_json"])
    assert trace and trace[0]["rule_id"] == "RECENT-01"


def test_no_base_fields_is_a_plain_passthrough():
    outcome = merge_claims(
        base_fields={"fields": []},
        new_fields={"fields": [_new("occupation", "医生")]},
        current_rows=[_row()],
    )
    assert [_c["value"] for _c in _fields(outcome)] == ["医生"]


def test_merge_output_carries_no_internal_bookkeeping_key():
    """内部标记不得漏进产物：投影层不认识它，留着只会让产物形状随实现变化。"""
    outcome = merge_claims(
        base_fields={"fields": []},
        new_fields={"fields": [_new("occupation", "医生")]},
        current_rows=[_row()],
    )
    for field in _fields(outcome):
        assert "_from_new" not in field

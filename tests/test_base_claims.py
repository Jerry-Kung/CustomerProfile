"""底座组装与补缺的三条规则（``01`` §2.4）。

这是整套增量方案里唯一「会退回上一版证据」的地方，也是唯一的质量让步所在，因此每条
规则都要正反例成对钉住：补错证据不会报错，只会让销售点开时看到一段对不上的依据。
"""

from __future__ import annotations

import json

from customer_profile.incremental.base_claims import (
    BASE_CHANNEL,
    assemble_base_claims,
    base_facts_signature,
)


def _profile(**notes) -> dict:
    return {"basic_notes": notes}


def _note(value, summary="", locked=False):
    return {"value": value, "reasoning_summary": summary, "is_locked": locked}


def _callback(notes: list[dict], *, level="B") -> dict:
    return {
        "phone": "13900000001",
        "batch_id": "b-1",
        "analysis_result": {
            "profile_summary": {"deal_level": level},
            "basic_notes_updates": notes,
            "inference_profile": [],
        },
    }


def _entry(key, value, *, evidence=(), steps=(), confidence=0.8):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {
            "summary": "上一版结论",
            "steps": list(steps),
            "evidence": list(evidence),
            "confidence": confidence,
        },
    }


def _fields(outcome) -> dict[str, dict]:
    return {item["attribute"]: item for item in json.loads(outcome["fields_json"])["fields"]}


def test_same_value_inherits_evidence_from_the_copy():
    """A 与 B 逐字相同：取值用 A，**证据与置信度沿用 B**。"""
    outcome = assemble_base_claims(
        _profile(occupation=_note("教师")),
        _callback([_entry("occupation", "教师", evidence=["销售跟进记录：我在学校教书"], confidence=0.9)]),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "教师"
    assert field["basis_of_inference"] == ["销售跟进记录：我在学校教书"]
    assert field["confidence"] == 0.9
    assert field["base_origin"] == "A=B"


def test_value_difference_uses_the_api_and_drops_the_old_evidence():
    """A 与 B 不同：以 A 为准，只剩 A 的 summary，**不臆造**证据与置信度。"""
    outcome = assemble_base_claims(
        _profile(occupation=_note("医生", summary="客户自述在医院工作")),
        _callback([_entry("occupation", "教师", evidence=["销售跟进记录：我在学校教书"], confidence=0.9)]),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "医生"
    assert field["basis_of_inference"] == ["客户自述在医院工作"]
    assert field["confidence"] is None
    assert field["base_origin"] == "A!=B"


def test_missing_in_api_falls_back_to_the_copy_entirely():
    """A 没有该项：B 的全部信息照用（含推导链与置信度）。"""
    outcome = assemble_base_claims(
        _profile(),
        _callback([_entry("occupation", "教师", steps=["客户自称老师"], confidence=0.7)]),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "教师"
    assert field["reasoning_chain"] == ["客户自称老师"]
    assert field["confidence"] == 0.7
    assert field["base_origin"] == "B_only"


def test_empty_profile_still_yields_a_usable_base():
    """22% 的读回没有 ``basic_notes``：底座整份来自副本。"""
    outcome = assemble_base_claims({}, _callback([_entry("hobbies", "钓鱼")]))
    assert outcome["stats"]["from_b_only"] == 1
    assert _fields(outcome)["hobbies"]["value"] == "钓鱼"


def test_locked_note_in_api_is_a_registry_level_fact():
    outcome = assemble_base_claims(
        _profile(customer_name=_note("张三", locked=True)),
        _callback([_entry("customer_name", "张三")]),
    )
    assert _fields(outcome)["customer_name"]["basis"] == "fact"


def test_all_base_claims_carry_the_base_channel():
    outcome = assemble_base_claims(
        _profile(age=_note("35-40")),
        _callback([_entry("age", "35-40")]),
    )
    assert _fields(outcome)["age"]["record_channel"] == BASE_CHANNEL


def test_structured_value_equality_is_compared_as_text():
    """结构化枚举在 A 里是对象、在 B 里是 JSON 串：值相同即视为相同，不该判成变化。"""
    outcome = assemble_base_claims(
        _profile(driver_license_status=_note({"key": "yes", "input_value": ""})),
        _callback([_entry("driver_license_status", json.dumps({"key": "yes", "input_value": ""}))]),
    )
    assert _fields(outcome)["driver_license_status"]["base_origin"] == "A=B"


def test_sections_are_carried_for_expression_skip():
    payload = _callback([_entry("age", "35-40")])
    payload["analysis_result"]["profile_summary"] = {"summary": "关注空间"}
    payload["analysis_result"]["usage_scenarios"] = ["家庭出行"]
    outcome = assemble_base_claims({}, payload)
    assert outcome["sections"]["usage_scenarios"] == ["家庭出行"]
    assert outcome["sections"]["profile_summary"] == {"summary": "关注空间"}


def test_facts_signature_ignores_key_order():
    assert base_facts_signature({"a": 1, "b": 2}) == base_facts_signature({"b": 2, "a": 1})
    assert base_facts_signature({"a": 1}) != base_facts_signature({"a": 2})


def test_stats_count_each_rule():
    outcome = assemble_base_claims(
        _profile(age=_note("35-40"), occupation=_note("医生")),
        _callback([_entry("age", "35-40"), _entry("occupation", "教师"), _entry("hobbies", "钓鱼")]),
    )
    assert outcome["stats"]["same"] == 1
    assert outcome["stats"]["differ"] == 1
    assert outcome["stats"]["from_b_only"] == 1

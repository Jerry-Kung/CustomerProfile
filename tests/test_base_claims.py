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
    """旧形状（≤2026-11-02）：``basic_notes`` 字典 + ``reasoning_summary``。"""
    return {"basic_notes": notes}


def _note(value, summary="", locked=False):
    return {"value": value, "reasoning_summary": summary, "is_locked": locked}


def _new_profile(*entries) -> dict:
    """新形状（2026-11-17 起）：``basic_notes_updates`` 列表，每条带完整推导。"""
    return {"basic_notes_updates": list(entries)}


def _new_note(key, value, *, locked=0, feedback=None, **detail):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {
            "summary": detail.pop("summary", "接口侧结论"),
            "steps": list(detail.pop("steps", ())),
            "evidence": list(detail.pop("evidence", ())),
            "confidence": detail.pop("confidence", 0.8),
        },
        "is_locked": locked,
        "ai_feedback": feedback,
    }


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


def test_value_difference_uses_the_api_and_keeps_the_old_derivation():
    """A 与 B 不同：以 A 为准，只有 A 的 summary，**不臆造**证据与置信度。

    **但推导链沿用 B**（2026-11-17）：A 读回的字段根本没有 steps，整条丢掉会让这个
    字段在出口退回代码兜底句。``steps`` 说的是「上回怎么想到这一层的」，取值换了它通常
    仍然成立；证据与置信度指的是旧值，不沿用。
    """
    outcome = assemble_base_claims(
        _profile(occupation=_note("医生", summary="客户自述在医院工作")),
        _callback(
            [
                _entry(
                    "occupation",
                    "教师",
                    evidence=["销售跟进记录：我在学校教书"],
                    steps=["客户提到带学生上课", "结论推断：职业记为教师"],
                    confidence=0.9,
                )
            ]
        ),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "医生"
    assert field["basis_of_inference"] == ["客户自述在医院工作"]
    assert field["confidence"] is None
    assert field["reasoning_chain"] == ["客户提到带学生上课", "结论推断：职业记为教师"]
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


# ---------------------------------------------------------------- 2026-11-17 新读回体


def test_new_list_container_is_read_as_notes():
    """新回传体把 Notes 换成 ``basic_notes_updates`` 列表——A 不能再读成空。"""
    outcome = assemble_base_claims(
        _new_profile(_new_note("occupation", "教师")),
        _callback([_entry("occupation", "教师")]),
    )
    assert outcome["stats"]["a_notes"] == 1
    assert _fields(outcome)["occupation"]["value"] == "教师"


def test_new_shape_prefers_a_own_reasoning_even_when_equal_to_b():
    """A 自带完整推导时，用它而不是 B 的——A 说的是**这次**的取值怎么来的。"""
    outcome = assemble_base_claims(
        _new_profile(
            _new_note(
                "occupation",
                "教师",
                steps=["识别到学校工作场景", "结论推断：职业记为教师"],
                evidence=["朋友圈：今天又是备课的一天"],
                confidence=0.86,
            )
        ),
        _callback(
            [_entry("occupation", "教师", evidence=["销售跟进记录：我在学校教书"], confidence=0.5)]
        ),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "教师"
    assert field["reasoning_chain"] == ["识别到学校工作场景", "结论推断：职业记为教师"]
    assert field["basis_of_inference"] == ["朋友圈：今天又是备课的一天"]
    assert field["confidence"] == 0.86
    assert field["base_origin"] == "A=B"


def test_new_shape_value_difference_uses_a_own_reasoning():
    """A≠B 以 A 为准时，推导链与置信度都取 A 的，不再沿用 B。"""
    outcome = assemble_base_claims(
        _new_profile(
            _new_note(
                "occupation",
                "医生",
                steps=["客户自述在医院工作"],
                evidence=["通话：我在医院上班"],
                confidence=0.9,
            )
        ),
        _callback(
            [_entry("occupation", "教师", evidence=["销售跟进记录：我在学校教书"], steps=["旧链条"], confidence=0.4)]
        ),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "医生"
    assert field["reasoning_chain"] == ["客户自述在医院工作"]
    assert field["basis_of_inference"] == ["通话：我在医院上班"]
    assert field["confidence"] == 0.9
    assert field["base_origin"] == "A!=B"


def test_new_shape_falls_back_to_b_when_a_lacks_detail():
    """A 有值但没给推导（真实现象：值在、``reasoning_detail`` 为 null）时仍用 B 的证据。"""
    note = _new_note("occupation", "教师")
    note["reasoning_detail"] = None
    outcome = assemble_base_claims(
        _new_profile(note),
        _callback([_entry("occupation", "教师", evidence=["销售跟进记录"], confidence=0.7)]),
    )
    field = _fields(outcome)["occupation"]
    assert field["value"] == "教师"
    assert field["basis_of_inference"] == ["销售跟进记录"]
    assert field["confidence"] == 0.7


def test_new_shape_null_value_and_detail_are_skipped():
    """142 条 Notes 的 ``attribute_value`` 与 ``reasoning_detail`` 同时为空：不成断言。"""
    outcome = assemble_base_claims(
        _new_profile(_new_note("hobbies", None)),
        _callback([]),
    )
    # 只剩卡片字段 ``deal_level``（来自回传体，不在 Notes 契约里）。
    assert set(_fields(outcome)) == {"deal_level"}


def test_new_shape_locked_note_is_registry_level():
    outcome = assemble_base_claims(
        _new_profile(_new_note("customer_name", "张三", locked=1)),
        _callback([_entry("customer_name", "张三")]),
    )
    assert _fields(outcome)["customer_name"]["basis"] == "fact"

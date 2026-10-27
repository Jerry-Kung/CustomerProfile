"""ENUM-01 与旧版枚举键别名表。

2026-10-27 旧键整改的验收用例。钉住三件事：

1. **别名表按口径映射**：``unmarried`` → ``unmarried_no_child``、``commute`` →
   ``family``、``easy-to-drive`` → ``easy_to_drive``；``within_6_months`` 与
   ``purchase_type`` 的旧值**刻意不在表里**（前者精度不可伪造，后者换字段）。
2. **归一层接得住旧键**：``normalize_enum`` 不再返回 ``None`` 把原值留库。
3. **闸门剔得干净**：降级后的非法枚举条整条剔除，合法取值与旧键映射值都留下。

反例组的重要性高于正例：把 ``within_6_months`` 硬映射成三档里的任何一档，都会让销售
按一个假精度逼单。
"""

from __future__ import annotations

from customer_profile import internal_fields as inf
from customer_profile import projection as proj
from customer_profile.rules.actions import BASIS_INFERRED_LOW
from customer_profile.rules.clusters import hard as hard_rules


def note(key, value, *, summary="", confidence=0.5):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {
            "summary": summary,
            "steps": [],
            "evidence": [],
            "confidence": confidence,
        },
    }


def gate(entries, basis=None):
    return hard_rules.check_notes(
        entries,
        basis=basis or {},
        is_conclusion=lambda _text: False,
        enum_known=inf.is_enum,
        legal_keys=inf.legal_keys,
        display_to_key=inf.display_to_key,
        is_enum_key=inf.looks_like_enum_key,
    )


# ---------------------------------------------------------------- 别名表

def test_family_structure_legacy_keys_map_to_no_child():
    """旧版只有 married/unmarried，新契约拆三档；旧值不含子女信息，落「无孩」最保守。"""
    assert inf.legacy_alias("family_structure", "married") == "married_no_child"
    assert inf.legacy_alias("family_structure", "unmarried") == "unmarried_no_child"


def test_purchase_purpose_legacy_keys_merge_into_family():
    """通勤与代步并入家用——新契约砍掉了这两项。"""
    assert inf.legacy_alias("purchase_purpose", "commute") == "family"
    assert inf.legacy_alias("purchase_purpose", "transport") == "family"


def test_hyphenated_driving_preference_is_corrected():
    """``easy-to-drive`` 是拼写错误，不是语义迁移。"""
    assert inf.legacy_alias("driving_preference", "easy-to-drive") == "easy_to_drive"


def test_within_six_months_is_deliberately_unmapped():
    """6 个月内与 3 个月内外/内都重叠但不等价，硬映射等于伪造精度。"""
    assert inf.legacy_alias("planned_purchase_time", "within_6_months") is None


def test_purchase_type_legacy_values_are_not_same_field_aliases():
    """首购/增购/换购是新契约不存在的取值，只能换字段，不能同字段映射。"""
    for value in ("first", "additional", "replacement"):
        assert inf.legacy_alias("purchase_type", value) is None


def test_purchase_type_legacy_values_route_to_purchase_nature():
    assert inf.legacy_cross_field("purchase_type", "replacement") == "purchase_nature"
    assert inf.legacy_cross_field("purchase_type", "首购") is None
    assert inf.legacy_cross_field("family_structure", "unmarried") is None


# ---------------------------------------------------------------- 归一层

def test_normalize_enum_accepts_legacy_key():
    assert proj.normalize_enum("family_structure", "unmarried") == "unmarried_no_child"
    assert proj.normalize_enum("purchase_purpose", "commute") == "family"


def test_normalize_enum_still_rejects_unmapped_value():
    assert proj.normalize_enum("planned_purchase_time", "within_6_months") is None
    assert proj.normalize_enum("core_concerns", "担心电池衰减") is None


def test_projection_routes_purchase_type_value_to_purchase_nature():
    """``purchase_type=replacement`` 不再以非法值留在 purchase_type，而是搬去购车性质。

    带上「有旧车证据」的旁证：``CONSIST-01`` 会对「声称换购却无旧车支撑」的购车性质
    整条剔除，那是跨字段一致性的独立职责，与本用例要验的「换字段」无关。
    """
    result = proj.project(
        [proj.Claim(attribute="purchase_type", value="replacement", channel="试驾录音信息")],
        context={"has_old_car_evidence": True},
    )
    assert result.facts.get("purchase_nature") == "replacement"
    assert not result.facts.get("purchase_type")


# ---------------------------------------------------------------- ENUM-01 闸门

def test_illegal_enum_after_downgrade_is_removed():
    """降级后仍非法：整条剔除，不把内部键回传给前端。"""
    actions = gate(
        [note("purchase_type", "replacement")],
        basis={"purchase_type": BASIS_INFERRED_LOW},
    )
    assert [a.rule for a in actions] == ["ENUM-01"]
    assert actions[0].action == "reject"


def test_legal_enum_after_downgrade_is_kept():
    """反例：合法键即使已降级也不该被剔——降级是置信问题，不是取值问题。"""
    actions = gate(
        [note("purchase_purpose", "family")],
        basis={"purchase_purpose": BASIS_INFERRED_LOW},
    )
    assert not [a for a in actions if a.rule == "ENUM-01"]


def test_legacy_key_is_not_treated_as_illegal():
    """反例：能映射到现行键的旧值不是非法值，不该被删。"""
    actions = gate(
        [note("family_structure", "married_no_child")],
        basis={"family_structure": BASIS_INFERRED_LOW},
    )
    assert not [a for a in actions if a.rule == "ENUM-01"]


def test_downgrade_before_removal_is_left_to_hard_02():
    """未降级的非法值归 HARD-02（降级转确认），不归 ENUM-01——两条规则各管一段。"""
    actions = gate([note("purchase_type", "replacement")], basis={"purchase_type": "fact"})
    rules = {a.rule for a in actions}
    assert "HARD-02" in rules
    assert "ENUM-01" not in rules


# ---------------------------------------------------------------- 回写投影的最后一道闸

def test_writeback_projection_drops_unrepairable_enum_entry():
    from customer_profile import callback_build as cb

    updates = cb.build_basic_notes_updates([
        note("purchase_type", "replacement"),
        note("family_structure", "unmarried"),
        note("customer_name", "客户1677"),
    ])
    keys = [item["attribute_key"] for item in updates]
    # 旧键能救的救回来（unmarried 有别名），救不回来的整条剔除，其余照发。
    assert keys == ["family_structure", "customer_name"]
    assert updates[0]["attribute_value"] == "unmarried_no_child"


def test_writeback_projection_keeps_text_field_untouched():
    """非枚举字段不参与取值校验——姓名、职业这类自由文本可以是任何字符串。"""
    from customer_profile import callback_build as cb

    updates = cb.build_basic_notes_updates([note("occupation", "To B 销售")])
    assert updates[0]["attribute_value"] == "To B 销售"

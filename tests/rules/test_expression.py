"""EXPR 簇：对客产出的语言形态。

钉住两件事，各有一组反例：

1. **枚举键在正文里必须被换成中文展示名**，但**品牌名、车型名、技术缩写不得动**——
   白名单的成立完全依赖「判据是键而非英文」这条设计（治理方案 §4.1）。
2. **英文占位串整条删除**，但 ``unknown`` 在 ``knowledge_of_*`` 是合法键，
   不能一视同仁。

反例组的重要性高于正例：一个误伤率高的替换规则会把客户原话改坏，而且它改的是
对客文案，销售看到的就是被改坏的那一版。
"""

from __future__ import annotations

from customer_profile import internal_fields as inf
from customer_profile.rules import NotesState, apply
from customer_profile.rules.clusters import expression as expr


def note(key, value, *, summary="", steps=None, evidence=None):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {
            "summary": summary,
            "steps": steps or [],
            "evidence": evidence or [],
            "confidence": 0.9,
        },
    }


def rewrite_actions(entries):
    return expr.check_prose_keys(entries)


# ---------------------------------------------------------------- EXPR-01 正例

def test_enum_key_in_prose_is_rewritten():
    """「已定稿事实：power、appearance」里的两个键都要变成中文。"""
    actions = rewrite_actions([note("core_concerns", "power", summary="已定稿事实：power、appearance")])
    assert [a.rule for a in actions] == ["EXPR-01"]
    detail = actions[0].payload["reasoning_detail"]
    assert detail["summary"] == "已定稿事实：动力、颜值"


def test_internal_enum_key_with_underscore_is_rewritten():
    """编外枚举（``emotion``）没有中文键形态，靠展示名索引同样能换。"""
    actions = rewrite_actions(
        [note("emotion", "guard_against", summary="已定稿事实：guard_against、straightforward")]
    )
    assert actions, "编外枚举键必须能被替换，否则决策心理三个 key 的正文仍然漏英文"
    assert actions[0].payload["reasoning_detail"]["summary"] == "已定稿事实：防备强、喜欢直接"


def test_multiple_prose_fields_are_rewritten_together():
    actions = rewrite_actions(
        [note("core_concerns", "smart", summary="已定稿事实：smart、safety", steps=["对比：safety"])]
    )
    detail = actions[0].payload["reasoning_detail"]
    assert detail["summary"] == "已定稿事实：智能、安全"
    assert detail["steps"] == ["对比：安全"]


def test_attribute_value_is_never_touched():
    """取值是契约枚举键，前端按 display_name 映射——改写会破坏回写。"""
    entry = note("family_structure", "unmarried_no_child", summary="已定稿事实：unmarried_no_child")
    state = NotesState(notes=[entry])
    apply(state, rewrite_actions(state.notes), max_rounds=1)
    assert state.notes[0]["attribute_value"] == "unmarried_no_child"
    assert state.notes[0]["reasoning_detail"]["summary"] == "已定稿事实：未婚无孩"


def test_evidence_is_never_touched():
    """证据位要能指回原文，改写会让锚点与原文对不上（HARD-03 的判据就是原文）。"""
    entry = note("education", "master", evidence=["客户原话：我是 master 毕业的"])
    assert rewrite_actions([entry]) == []


# ---------------------------------------------------------------- EXPR-01 反例

def test_brand_name_in_prose_is_not_rewritten():
    """品牌与车型名不是任何字段的合法键，必须原样保留。"""
    for text in ("客户关注 Model Y 与 BMW", "已试驾猛士M817和红旗G919", "搭载 ADS 智驾"):
        assert rewrite_actions([note("intended_model", "", summary=text)]) == [], text


def test_business_slang_is_not_rewritten():
    """``to B`` / ``max版`` 是行业通行说法，硬译成中文反而失真。"""
    for text in ("客户从事 to B 销售", "销售介绍 max版 34.99万", "老婆给换 18Pro Max"):
        assert rewrite_actions([note("occupation", "", summary=text)]) == [], text


def test_key_shaped_brand_inside_english_sentence_is_not_rewritten():
    """空格包围的普通英文不被当作枚举键——``smart`` 既是键也是品牌。"""
    assert rewrite_actions([note("core_concerns", "", summary="他开 smart 精灵")]) == []


def test_key_shaped_internal_token_survives_space_boundary():
    """带下划线的键形态与英文占位串在空格边界下仍算键——它们不可能是品牌名。"""
    actions = rewrite_actions([note("emotion", "", summary="情绪 guard_against 明显")])
    assert actions[0].payload["reasoning_detail"]["summary"] == "情绪 防备强 明显"


def test_clean_prose_produces_no_action():
    assert rewrite_actions([note("core_concerns", "power", summary="客户明确关注动力")]) == []


# ---------------------------------------------------------------- EXPR-03 占位串

def test_english_placeholder_is_rejected():
    actions = expr.check_placeholders(
        [note("payment_method", "unknown")], is_placeholder=inf.is_english_placeholder
    )
    assert [a.rule for a in actions] == ["EXPR-03"]
    assert actions[0].action == "reject"


def test_legal_unknown_key_is_kept():
    """``knowledge_of_mengshi = unknown`` 是合法取值（「未知」），不是占位串。"""
    actions = expr.check_placeholders(
        [note("knowledge_of_mengshi", "unknown")], is_placeholder=inf.is_english_placeholder
    )
    assert actions == []


def test_chinese_placeholder_is_left_to_hard_01():
    """中文占位串归 HARD-01，两张表不得互相吞并——否则同一条会被删两次、留痕重复。"""
    actions = expr.check_placeholders(
        [note("hobbies", "未提供")], is_placeholder=inf.is_english_placeholder
    )
    assert actions == []


def test_structured_enum_is_not_a_placeholder():
    actions = expr.check_placeholders(
        [note("driver_license_status", {"key": "yes", "input_value": ""})],
        is_placeholder=inf.is_english_placeholder,
    )
    assert actions == []

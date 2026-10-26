"""动作执行器：排序、应用、收敛、留痕。

这一层的失败形态同样是静默的：动作应用顺序错了，两条规则互相覆盖，最后一次写的
赢，而运行显示成功。因此每个用例都同时钉住「改成了什么」与「留痕记了什么」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from customer_profile import projection as proj
from customer_profile.rules import Action, Rule, RuleRegistry, apply, order
from customer_profile.rules import actions as act


@dataclass
class State:
    """执行器只按属性名访问状态，不 import ``Projection``。这里用最小同形对象。"""

    facts: dict[str, Any] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)
    to_confirm: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    downgraded: list[dict] = field(default_factory=list)


def make_rule(rule_id, kind="consistency", scope=("*",), **overrides):
    fields = dict(
        id=rule_id,
        version=3,
        title="t",
        content_kind=kind,
        channels=("code",),
        scope=scope,
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal="m",
        message_customer="c",
        owner="engineering",
        status="active",
        tests=("a", "b"),
    )
    fields.update(overrides)
    return Rule(**fields)


def table_of(*rules):
    table = RuleRegistry()
    for rule in rules:
        table.register(rule)
    return table


def action(rule, attribute="annual_income", kind="downgrade", **kwargs):
    fields = dict(
        rule=rule,
        attribute=attribute,
        action=kind,
        severity="repair",
        reason="测试理由",
    )
    fields.update(kwargs)
    return Action(**fields)


# ---------------------------------------------------------------- 常量同值

def test_basis_constants_match_projection():
    """规则包不 import 投影层，三分类字面量各写一份，因此必须由测试钉住同值。"""
    assert act.BASIS_FACT == proj.FACT
    assert act.BASIS_INFERRED_HIGH == proj.INFERRED_HIGH
    assert act.BASIS_INFERRED_LOW == proj.INFERRED_LOW


# ---------------------------------------------------------------- 排序

def test_order_puts_hard_before_consistency_before_spec():
    table = table_of(
        make_rule("SPEC-01", kind="spec"),
        make_rule("CONSIST-01", kind="consistency"),
        make_rule("EXPR-01", kind="hard"),
    )
    ordered = order(
        [
            action("SPEC-01"),
            action("CONSIST-01"),
            action("EXPR-01"),
        ],
        table,
    )
    assert [item.rule for item in ordered] == ["EXPR-01", "CONSIST-01", "SPEC-01"]


def test_order_breaks_ties_by_field_scope_then_id():
    table = table_of(
        make_rule("INCOME-01", scope=("*",)),
        make_rule("INCOME-02", scope=("annual_income", "occupation")),
        make_rule("INCOME-03", scope=("annual_income",)),
    )
    ordered = order(
        [
            action("INCOME-01"),
            action("INCOME-03"),
            action("INCOME-02"),
        ],
        table,
    )
    # 字段级（01 的 wildcard 之外）优先，同级按编号。
    assert [item.rule for item in ordered] == ["INCOME-02", "INCOME-03", "INCOME-01"]


def test_order_deduplicates_identical_actions():
    table = table_of(make_rule("INCOME-01"))
    same = action("INCOME-01")
    assert len(order([same, same], table)) == 1


# ---------------------------------------------------------------- 应用

def test_reject_removes_value_and_keeps_evidence():
    state = State(facts={"purchase_nature": "replacement"}, basis={"purchase_nature": "fact"})
    result = apply(state, [action("CONSIST-01", "purchase_nature", "reject")], registry=table_of(make_rule("CONSIST-01")))
    assert "purchase_nature" not in state.facts
    assert "purchase_nature" not in state.basis
    assert state.rejected[0]["rule"] == "CONSIST-01"
    assert result.traces[0].before == "replacement"
    assert result.traces[0].after is None


def test_downgrade_keeps_value_and_lowers_basis():
    state = State(facts={"annual_income": "30-50万"}, basis={"annual_income": "fact"})
    apply(state, [action("INCOME-01")], registry=table_of(make_rule("INCOME-01")))
    assert state.facts["annual_income"] == "30-50万"
    assert state.basis["annual_income"] == proj.INFERRED_LOW
    assert state.downgraded[0]["rule"] == "INCOME-01"


def test_rewrite_marks_basis_as_inferred_high():
    state = State(facts={"deal_level": "5档收入"}, basis={"deal_level": "fact"})
    apply(
        state,
        [action("INCOME-03", "deal_level", "rewrite", value="40-60万")],
        registry=table_of(make_rule("INCOME-03")),
    )
    assert state.facts["deal_level"] == "40-60万"
    assert state.basis["deal_level"] == proj.INFERRED_HIGH


def test_require_confirm_is_idempotent_per_rule():
    table = table_of(make_rule("INCOME-01"))
    state = State(facts={"annual_income": "30-50万"})
    entry = action("INCOME-01", "annual_income", "require_confirm", customer_reason="确认是否有第二收入")
    apply(state, [entry], registry=table)
    apply(state, [entry], registry=table)
    assert len(state.to_confirm) == 1
    assert state.to_confirm[0]["reason"] == "确认是否有第二收入"


def test_annotate_does_not_touch_facts_but_is_traced():
    state = State(facts={"deal_level": "待识别"})
    result = apply(
        state,
        [action("LEVEL-04", "deal_level", "annotate")],
        registry=table_of(make_rule("LEVEL-04")),
    )
    assert state.facts == {"deal_level": "待识别"}
    assert result.traces[0].action == "annotate"
    assert result.noop == 1


def test_action_on_missing_field_is_traced_and_counted():
    """命中但没改到东西也要留痕——规则失效的信号就藏在这里。"""
    state = State()
    result = apply(
        state,
        [action("CONSIST-01", "purchase_nature", "reject")],
        registry=table_of(make_rule("CONSIST-01")),
    )
    assert result.noop == 1
    assert len(result.traces) == 1
    assert result.traces[0].attribute == "purchase_nature"


# ---------------------------------------------------------------- 收敛

def test_check_runs_until_no_new_actions():
    table = table_of(make_rule("INCOME-01"), make_rule("CONSIST-01"))
    state = State(facts={"annual_income": "30-50万", "purchase_nature": "replacement"})
    calls: list[int] = []

    def check(_state):
        calls.append(1)
        # 第一轮复查发现换购无证据，产生第二条动作；之后不再产生。
        if len(calls) == 1:
            return [action("CONSIST-01", "purchase_nature", "reject")]
        return []

    result = apply(state, [action("INCOME-01")], check=check, registry=table)
    assert result.unstable is False
    assert result.rounds == 2
    assert "purchase_nature" not in state.facts


def test_unstable_is_recorded_when_rules_never_settle():
    table = table_of(make_rule("INCOME-01"))
    state = State(facts={"annual_income": "30-50万"})
    counter = {"n": 0}

    def check(_state):
        counter["n"] += 1
        # 每一轮都产生同一字段上「不同取值」的改写 → 永不收敛。
        return [action("INCOME-01", "annual_income", "rewrite", value=f"值{counter['n']}")]

    result = apply(state, [action("INCOME-01")], check=check, max_rounds=2, registry=table)
    assert result.unstable is True
    assert any("[unstable]" in trace.reason for trace in result.traces)


def test_converging_exactly_on_last_round_is_not_unstable():
    table = table_of(make_rule("INCOME-01"))
    state = State(facts={"annual_income": "a"})
    counter = {"n": 0}

    def check(_state):
        counter["n"] += 1
        if counter["n"] < 2:
            return [action("INCOME-01", "annual_income", "rewrite", value=f"v{counter['n']}")]
        return []

    result = apply(state, [action("INCOME-01")], check=check, max_rounds=2, registry=table)
    assert result.unstable is False


def test_trace_carries_rule_version_and_source():
    table = table_of(make_rule("INCOME-01", version=7))
    state = State(facts={"annual_income": "30-50万"})
    result = apply(state, [action("INCOME-01")], registry=table, source="llm_checker")
    trace = result.traces[0]
    assert trace.version == 7
    assert trace.source == "llm_checker"

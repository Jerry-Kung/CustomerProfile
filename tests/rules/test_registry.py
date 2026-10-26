"""登记表自检与灰度。

这一层的错误都是「静默」的：编号写错不会报错，只是永远检索不到；``active`` 没测试
不会报错，只是没人验证过；两条规则共用一个旧裸串不会报错，只是对账时分不清归属。
因此这里的用例全部围绕**启动期就该抛**的那几类。
"""

from __future__ import annotations

import pytest

from customer_profile.rules import Rule, RuleRegistry, RuleTableError
from customer_profile.rules import registry as reg


def make_rule(rule_id="INCOME-01", **overrides):
    fields = dict(
        id=rule_id,
        version=1,
        title="一条测试规则",
        content_kind="consistency",
        channels=("code",),
        scope=("annual_income",),
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal="对内解释",
        message_customer="需要您确认一下",
        owner="engineering",
        status="active",
        cluster="income",
        tests=("tests/rules/test_x.py::test_yes", "tests/rules/test_x.py::test_no"),
    )
    fields.update(overrides)
    return Rule(**fields)


# ---------------------------------------------------------------- 字段形态

def test_id_must_look_like_cluster_number():
    assert reg.ID_PATTERN.match("INCOME-01")
    assert reg.ID_PATTERN.match("CONSIST-03")
    for bad in ("income-01", "INCOME01", "INCOME_01", "01"):
        with pytest.raises(ValueError):
            make_rule(rule_id=bad)


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_kind", "expressive"),
        ("channels", ("regex",)),
        ("stages", ("extract", "extract")),
        ("severity", "fatal"),
        ("default_action", "delete"),
        ("owner", "vendor"),
        ("status", "done"),
        ("scope", ()),
        ("message_internal", "  "),
    ],
)
def test_illegal_field_is_rejected_at_construction(field, value):
    with pytest.raises(ValueError):
        make_rule(**{field: value})


def test_customer_message_required_only_for_require_confirm():
    """只有 ``require_confirm`` 会把消息写进下发的 ``to_confirm``，因此只有它必填。"""
    for non_facing in ("reject", "downgrade", "annotate", "rewrite"):
        assert make_rule(default_action=non_facing, message_customer="").id == "INCOME-01"
    with pytest.raises(ValueError):
        make_rule(default_action="require_confirm", message_customer="   ")


def test_version_must_be_positive():
    with pytest.raises(ValueError):
        make_rule(version=0)


# ---------------------------------------------------------------- 登记

def test_duplicate_id_is_rejected():
    table = RuleRegistry()
    table.register(make_rule())
    with pytest.raises(RuleTableError):
        table.register(make_rule())


def test_legacy_code_cannot_be_shared():
    """两条规则共用一个旧裸串，对账时分不清这次命中该记到谁头上。"""
    table = RuleRegistry()
    table.register(make_rule(legacy_codes=("f_trade_in",)))
    with pytest.raises(RuleTableError):
        table.register(make_rule("CONSIST-01", legacy_codes=("f_trade_in",)))


def test_legacy_code_resolves_to_rule():
    table = RuleRegistry()
    table.register(make_rule("INCOME-02", legacy_codes=("e_non_income_tag",)))
    assert table.resolve_legacy("e_non_income_tag").id == "INCOME-02"
    assert table.resolve_legacy("nothing_here") is None


# ---------------------------------------------------------------- 自检

def test_active_rule_without_two_tests_fails_verification():
    table = RuleRegistry()
    table.register(make_rule(tests=("only_one",)))
    issues = table.verify()
    assert any("正反例测试不足" in item for item in issues)
    with pytest.raises(RuleTableError):
        table.require_valid()


def test_proposed_rule_may_skip_tests():
    """未生效的规则不要求测试；一经置 active，自检就会拦。"""
    table = RuleRegistry()
    table.register(make_rule(status="proposed", tests=()))
    assert table.verify() == []


def test_default_registry_loads_and_is_cached():
    reg.reset_default()
    first = reg.default()
    assert first is reg.default()
    assert first.verify() == []
    reg.reset_default()
    assert reg.default() is not first


# ---------------------------------------------------------------- 灰度

def test_without_disables_single_rule_only():
    table = RuleRegistry()
    table.register(make_rule("INCOME-01"))
    table.register(make_rule("INCOME-02"))
    trimmed = table.without(["INCOME-01"])
    assert [rule.id for rule in trimmed.all()] == ["INCOME-02"]
    # 原表不受影响：灰度是「算一份新表」，不是就地删条目。
    assert len(table) == 2


def test_without_ignores_unknown_ids():
    table = RuleRegistry()
    table.register(make_rule())
    assert len(table.without(["RETIRED-99"])) == 1
    assert table.without([]) is table

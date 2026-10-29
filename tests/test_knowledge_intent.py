"""意向车型闭集与四路分流（K5，规则 ``KB-02``）。

这是本轮业务口径的落点，也是最需要正反例成对钉住的一条：

- 正例：竞品车型**不能**留在意向位，但**必须**落到对比位；
- 反例：本公司未在售的老型号、完全陌生的称谓**不得**被塞进竞品位——
  未知不等于竞品，代码替模型制造事实比留白更糟。

最容易被写错的一条在文件末尾：客户说「我现在开 917，想再买一台」时，
那个 917 **必须留在意向位**。
"""

from __future__ import annotations

import pytest

from customer_profile import knowledge
from customer_profile.knowledge import intent
from customer_profile.knowledge.pack import DEFAULT_PACK_DIR, load_pack
from customer_profile.workflows import customer_profile_pipeline as pipe


@pytest.fixture(scope="module")
def pack():
    return load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")


def make_claim(attribute, value, channel="试驾录音信息"):
    """最简断言对象：K5 按属性名访问，不依赖投影层。"""
    from customer_profile.projection import Claim

    return Claim(attribute=attribute, value=value, channel=channel)


def values_of(outcome, attribute):
    return [c.value for c in outcome.claims if c.attribute == attribute]


# ---------------------------------------------------------------- 正例：闭集内

def test_on_sale_model_is_kept(pack):
    """客户说想买 M817：它在清单里，留在意向位。"""
    outcome = intent.reroute_claims([make_claim("intended_model", "猛士M817")], pack)
    assert values_of(outcome, "intended_model") == ["猛士M817"]
    assert not outcome.touched


def test_alias_of_on_sale_model_is_kept(pack):
    """客户说的是别名（917），同样留在意向位。"""
    outcome = intent.reroute_claims([make_claim("intended_model", "917")], pack)
    assert values_of(outcome, "intended_model") == ["917"]


# ---------------------------------------------------------------- 正例：竞品改投

def test_competitor_model_is_rerouted(pack):
    """非在售车型从意向位移出，**改投对比车型位**——信息不能丢。

    这条是 ``KB-02`` 的登记测试引用之一：它钉住「闭集的代价不是丢信息」。

    同时转一条确认：代码能确定「它不是我们的在售车型」，不能确定「客户在比较它
    还是在开它」——后者取决于客户原话，因此改投之后仍请人核一眼。
    """
    outcome = intent.reroute_claims([make_claim("intended_model", "坦克700")], pack)
    assert "intended_model" not in values_of(outcome, "intended_model")
    assert values_of(outcome, "competing_model") == ["坦克700"]
    assert len(outcome.rerouted) == 1
    assert len(outcome.to_confirm) == 1, "改投照做，结论请人确认"


# ---------------------------------------------------------------- 反例：不擅自归类

def test_unknown_name_is_not_forced_into_competing(pack):
    """陌生称谓**绝不留在意向位**，且必须转确认让人来看。

    这是 ``KB-02`` 的另一条登记测试引用。它钉住的是「不得编造归属」：客户提到的车
    可能是在对比、也可能是在说自己那台，代码看不出语气，所以只把它按对比记录并
    请人确认，不在没有依据时把它写成确定结论。
    """
    outcome = intent.reroute_claims([make_claim("intended_model", "某不知名牌子X9")], pack)
    assert values_of(outcome, "intended_model") == []
    assert len(outcome.to_confirm) == 1
    assert outcome.rerouted, "信息要保留在对比位，不能凭空丢"""


def test_our_brand_unknown_model_goes_to_confirm_not_competing(pack):
    """本方品牌但型号不在售（如「老款 917 特别版」）→ 转确认，同样不进竞品位。"""
    outcome = intent.reroute_claims(
        [make_claim("intended_model", "猛士老款917特别版")], pack
    )
    assert values_of(outcome, "competing_model") == []
    assert outcome.dropped[0]["kind"] == "our_brand_unknown_model"
    assert outcome.to_confirm


# ---------------------------------------------------------------- 关键反例：同款再购

def test_owned_same_model_is_not_dropped(pack):
    """**客户说「我现在开 917，想再买一台」：917 必须留在意向位。**

    代码看不出语气，因此只要该值同时出现在自用车位，就**不剔除**，转人工确认让销售
    判断。这条宁留勿错——误剔除会把真实需求抹掉，而多一条确认只是让销售多看一眼。
    """
    outcome = intent.reroute_claims(
        [
            make_claim("old_car_model", "917"),
            make_claim("intended_model", "917"),
        ],
        pack,
    )
    assert values_of(outcome, "intended_model") == ["917"]
    assert not outcome.dropped
    assert any("再购" in item["reason"] or "已有车辆" in item["reason"]
               for item in outcome.to_confirm)


def test_owned_different_model_does_not_affect_intent(pack):
    """自用车型与意向车型不同时，互不影响。"""
    outcome = intent.reroute_claims(
        [
            make_claim("old_car_model", "别克君威"),
            make_claim("intended_model", "猛士M817"),
        ],
        pack,
    )
    assert values_of(outcome, "intended_model") == ["猛士M817"]
    assert not outcome.touched


# ---------------------------------------------------------------- 多值

def test_multiple_intended_values_each_judged(pack):
    """多值逐项判定：在售的留、竞品的改投、陌生的转确认。"""
    outcome = intent.reroute_claims(
        [make_claim("intended_model", ["猛士M817", "坦克700"])], pack
    )
    assert values_of(outcome, "intended_model") == ["猛士M817"]
    assert values_of(outcome, "competing_model") == ["坦克700"]


# ---------------------------------------------------------------- 退化与开关

def test_empty_pack_is_passthrough():
    """空包时原样返回——未接知识就不能有闭集约束。"""
    claims = [make_claim("intended_model", "坦克700")]
    outcome = intent.reroute_claims(claims, knowledge.EMPTY_PACK)
    assert values_of(outcome, "intended_model") == ["坦克700"]
    assert not outcome.touched


def test_no_intended_claim_is_noop(pack):
    outcome = intent.reroute_claims([make_claim("occupation", "教师")], pack)
    assert not outcome.touched


def test_trace_shape_matches_rule_trace(pack):
    """留痕字段与 ``RuleTrace`` 对齐，否则统计脚本读不到。"""
    outcome = intent.reroute_claims([make_claim("intended_model", "坦克700")], pack)
    trace = outcome.as_trace()[0]
    assert trace["rule_id"] == "KB-02"
    assert {"stage", "attribute", "action", "severity", "reason", "source",
            "before", "after", "version"} <= set(trace)


# ---------------------------------------------------------------- 管线接线

def test_pipeline_disables_kb02_by_rule_switch(monkeypatch):
    """``RULES_DISABLED=KB-02`` 时不改归属——单条规则可单独回滚。"""
    import json

    claims = json.dumps(
        {"claims": [{
            "claim_id": "c_001", "subject": "customer", "attribute": "intended_model",
            "value": "坦克700", "record_channel": "试驾录音信息",
            "anchor": "我也看了坦克700", "claim_type": "fact", "confidence": 0.9,
        }]},
        ensure_ascii=False,
    )
    monkeypatch.setattr(pipe, "_disabled_rule_ids", lambda: ("KB-02",))
    result = pipe.project_claims(claims)
    assert "坦克700" in json.loads(result["facts_json"])["intended_model"]

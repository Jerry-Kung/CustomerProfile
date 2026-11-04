"""落点用例：给已有正例补上对应的**反例**。

位于 ``tests/rules/`` 而不是塞进 ``tests/test_projection.py``：这一份只为满足
「``active`` 规则必须有正反例」这条自检，断言的是**规则没被误触发**，与投影层的行为
测试分开，便于日后整体迁移时一起搬走。
"""

from __future__ import annotations

from customer_profile import projection as proj


def claim(attribute, value, channel="试驾录音信息", **kwargs):
    return proj.Claim(attribute=attribute, value=value, channel=channel, **kwargs)


def test_company_kept_when_not_deregistered():
    """CONSIST-03 的反例：企业正常存续时，company 必须照常写入。"""
    result = proj.project([claim("company", "广东某某科技有限公司", channel="关联企业信息")])
    assert result.facts.get("company") == "广东某某科技有限公司"
    assert not any(r.get("rule") == "f_deregistered" for r in result.rejected)


def test_owned_car_claim_kept_when_not_a_deposit():
    """CONSIST-04 的反例：客户说自己在开的车照常写进 ``old_car_model``。

    判据只看「有没有下单词」，不看字段本身——真自用车被误杀等于把最基础的销售信息
    丢掉。
    """
    result = proj.project(
        [
            claim(
                "old_car_model",
                "2019款别克君威",
                quote="我现在开的是一九年的君威",
            )
        ]
    )
    assert result.facts.get("old_car_model") == "2019款别克君威"
    assert not any(r.get("rule") == "f_deposit" for r in result.rejected)


def test_no_car_kept_without_evidence():
    """CONSIST-05 反例：没有旧车 / 置换凭据时，「无车」是正确结论，不得改写。

    首购客户本来就没车。误改会凭空给客户添一台车，且购车性质会跟着被带成换购。
    """
    result = proj.project(
        [claim("current_vehicle_info", "无车", channel="第三方画像数据")]
    )
    assert result.facts.get("current_vehicle_info") == "无车"
    assert not any(t.get("rule_id") == "CONSIST-05" for t in result.rule_trace)


def test_trade_in_model_kept_with_requirement():
    """CONSIST-06 的反例：置换需求成立时，置换车型照常写入。

    判据只看「置换需求是不是 yes」，不看这句话是从哪来的——真置换客户被误杀等于把
    最直接的成交线索丢掉。
    """
    result = proj.project(
        [
            claim(
                "trade_in_requirement",
                '{"key": "yes", "input_value": "问界"}',
                quote="我这台问界想置换掉",
            ),
            claim(
                "trade_in_model",
                "问界M7",
                quote="就这台问界M7，你们评估一下",
            ),
        ]
    )
    assert result.facts.get("trade_in_model") == "问界M7"
    assert not any(
        r.get("rule") == "f_trade_in_model_without_need" for r in result.rejected
    )


def test_trade_in_requirement_no_still_blocks_model():
    """CONSIST-06：客户明确说「不置换」时，置换车型更不该留。

    这一形态在 UAT 归档里有 38 例，说明文字多为「客户未提及旧车/置换」——把没提到
    当成了没有，随后又补了一台车。
    """
    result = proj.project(
        [
            claim("trade_in_requirement", "no", quote="我这车不置换"),
            claim(
                "trade_in_model",
                "极氪",
                basis="inferred_low",
                basis_of_inference=("现车为极氪",),
            ),
        ]
    )
    assert "trade_in_model" not in result.facts


def test_single_level_is_not_touched():
    """LEVEL-01 的反例：只有一个来源给出等级时，不产生冲突、不改值、不转确认。"""
    result = proj.project([claim("deal_level", "B")])
    assert result.facts.get("deal_level") == "B"
    assert not any(r.get("rule") == "f_level" for r in result.rejected)
    assert not any(item.get("attribute_key") == "deal_level" for item in result.to_confirm)

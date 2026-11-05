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


def test_placeholder_old_car_does_not_rewrite_current_vehicle():
    """CONSIST-05 反例：旧车字段是占位串时，不得据此把「无车」改写成「有车」。

    UAT `13720382502` 的根因就在这里：``old_car_model`` 全是「未知」，判据却当成
    「客户有台车」，现车信息被改成「有车（具体车型与车龄待核实）」——凭据是空的，
    改出来的结论却是确定的。四种占位形态都要挡住。
    """
    for value in ("未知", "未提供", "无（第三方预测倾向无车）", "无车", "暂无", "未知车型"):
        result = proj.project(
            [
                claim("old_car_model", value, channel="第三方画像数据"),
                claim("current_vehicle_info", "无车", channel="第三方画像数据"),
            ]
        )
        assert result.facts.get("current_vehicle_info") == "无车", value
        assert not any(t.get("rule_id") == "CONSIST-05" for t in result.rule_trace), value


def test_real_old_car_still_rewrites_current_vehicle():
    """正例对照：旧车字段真的是台车时，改写照做——不能把规则整条关掉。"""
    result = proj.project(
        [
            claim("old_car_model", "2019款别克君威", quote="我现在开的是一九年的君威"),
            claim("current_vehicle_info", "无车", channel="第三方画像数据"),
        ]
    )
    assert result.facts.get("current_vehicle_info") == "有车（具体车型与车龄待核实）"
    assert any(t.get("rule_id") == "CONSIST-05" for t in result.rule_trace)


def test_placeholder_old_car_does_not_inject_driver_license():
    """旁证侧同口径：占位串不得被当成「有旧车」而注入「有驾照」。"""
    from customer_profile.workflows.customer_profile_pipeline import _has_old_car

    assert _has_old_car([{"attribute": "old_car_model", "value": "未知"}]) is False
    assert _has_old_car([{"attribute": "old_car_model", "value": "无（倾向无旧车）"}]) is False
    assert _has_old_car([{"attribute": "old_car_model", "value": "奔驰CLA"}]) is True


def test_trade_in_yes_with_brand_counts_as_evidence():
    """契约里 ``yes`` 带的品牌输入框（``yes（长安）``）必须与裸 ``yes`` 同权。

    只看全等会把 39 例真实置换判成「无凭据」，CONSIST-01 随即把购车性质撤回——
    客户的置换线索被抹掉，而这条线索正是销售要用来压价的。
    """
    for value in ("yes", "yes（长安）", {"key": "yes", "input_value": "长安"}):
        result = proj.project(
            [
                claim("old_car_model", "长安", channel="试驾录音信息"),
                claim("trade_in_requirement", value, channel="试驾录音信息"),
                claim("purchase_nature", "replacement", channel="试驾录音信息"),
            ]
        )
        assert result.facts.get("purchase_nature") == "replacement", value
        assert not any(r.get("rule") == "f_trade_in" for r in result.rejected), value


def test_trade_in_no_still_withdraws_unbacked_replacement():
    """反例：置换需求为「无」时，无凭据的「换购」照常撤回——不能把规则改松。"""
    result = proj.project(
        [
            claim("old_car_model", "未知"),
            claim("trade_in_requirement", "no", channel="试驾录音信息"),
            claim("purchase_nature", "replacement", channel="试驾录音信息"),
        ]
    )
    assert result.facts.get("purchase_nature") != "replacement"

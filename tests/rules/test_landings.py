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


def test_single_level_is_not_touched():
    """LEVEL-01 的反例：只有一个来源给出等级时，不产生冲突、不改值、不转确认。"""
    result = proj.project([claim("deal_level", "B")])
    assert result.facts.get("deal_level") == "B"
    assert not any(r.get("rule") == "f_level" for r in result.rejected)
    assert not any(item.get("attribute_key") == "deal_level" for item in result.to_confirm)

"""知识视图（K3 取证侧、K4 表达侧）。

两个视图的判据不是「写得好不好看」，而是三条硬边界：

1. **空包输出空串** —— 这是整个知识能力的回滚口，必须逐字节退回接入前；
2. **K3 只给称谓，不给参数** —— 给 L1 配置参数会诱使它把参数写进结论；
3. **K4 必须自述「知识不是事实」** —— 否则模型会把知识里的价格当客户已表达的偏好。
"""

from __future__ import annotations

import pytest

from customer_profile import knowledge
from customer_profile.knowledge import views
from customer_profile.knowledge.pack import DEFAULT_PACK_DIR, load_pack


@pytest.fixture(scope="module")
def pack():
    return load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")


# ================================================================
# K3 · 取证侧视图
# ================================================================

def test_empty_pack_renders_empty_string():
    """空包 → 空串。``build_l1_input`` 据此逐字节回到接入前。"""
    assert views.render_brand_brief(knowledge.EMPTY_PACK) == ""
    assert views.render_product_brief({}, knowledge.EMPTY_PACK) == ""


def test_brief_lists_every_on_sale_model(pack):
    """清单必须完整：漏一个别名，客户说出的合法车型就会被判成非在售。"""
    text = views.render_brand_brief(pack)
    for entry in pack.models:
        assert entry.canonical_name in text
        for alias in entry.aliases:
            assert alias in text


def test_brief_states_the_closure_rule(pack):
    """闭集口径要写在视图里——它是模型侧的事前引导。"""
    text = views.render_brand_brief(pack)
    assert "意向车型只能从" in text


def test_brief_does_not_leak_prices(pack):
    """**K3 不给价格**：L1 的任务是归类，不是讲解。给参数会诱使它把参数写进结论。"""
    text = views.render_brand_brief(pack)
    assert "万元" not in text
    assert "21.98" not in text and "29.99" not in text


def test_brief_marks_coming_models_if_any(pack):
    """待上市车型仍属本方意向范围，但不能被说成「在售」。"""
    text = views.render_brand_brief(pack)
    coming = [e for e in pack.models if e.status == "coming"]
    if coming:
        assert "尚未正式开售" in text


# ================================================================
# K4 · 表达侧视图
# ================================================================

def test_product_brief_marks_knowledge_vs_fact(pack):
    """硬边界自述：视图里的价格是产品知识，不是客户已表达的偏好。"""
    text = views.render_product_brief({}, pack)
    assert "不是客户已表达的事实" in text
    assert "不得" in text


def test_product_brief_gives_full_entry_for_mentioned_model(pack):
    """客户提到的车型给完整条目：卖点、档位、话术都在。"""
    text = views.render_product_brief({"intended_model": "猛士M817"}, pack)
    assert "猛士M817" in text
    assert "非承载大梁底盘" in text, "卖点应出现"
    assert "Mate" in text, "档位表应出现"
    assert "推介话术参考" in text


def test_product_brief_resolves_alias(pack):
    """客户说别名（917）也要能选中条目。"""
    assert views.mentioned_models({"intended_model": "917"}, pack) == ["dfms-917"]


def test_product_brief_full_entry_only_for_hit_models(pack):
    """命中的条目给完整卖点，其它只给一行速览或干脆不给。"""
    text = views.render_product_brief({"intended_model": "猛士M817"}, pack)
    assert "非承载大梁底盘" in text
    # 917 的独有卖点不该出现在「本案相关」里
    assert "蟹行横向行驶模式" not in text or "可作参照" in text


def test_product_brief_falls_back_to_full_lineup_when_nothing_hits(pack):
    """一个都没命中 → 全系速览，仍让模型有资料可用。"""
    text = views.render_product_brief({"occupation": "教师"}, pack)
    assert "全系速览" in text
    for entry in pack.models:
        assert entry.canonical_name in text


def test_product_brief_is_bounded(pack):
    """注入量要可控：无差别塞 3000 字是现状，筛后应显著变小。"""
    text = views.render_product_brief(
        {"intended_model": "猛士M817", "budget_range": "30-40万"}, pack
    )
    assert len(text) <= 2000, f"单案注入 {len(text)} 字，超出预算"


def test_budget_parsing_does_not_crash_on_noise(pack):
    """预算字段形态多样（「30万」「30-50万」「暂无」），不得抛异常。"""
    for value in ("30万", "30-50万", "暂无", "", None, "预算待定", 35):
        views.render_product_brief({"budget_range": value}, pack)


def test_owned_car_also_selects_entry(pack):
    """自用车也要给资料：销售需要知道客户现在开什么、要不要置换。"""
    assert views.mentioned_models({"old_car_model": "猛士917"}, pack) == ["dfms-917"]

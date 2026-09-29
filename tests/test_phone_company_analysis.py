"""``手机号&企业信息分析`` 的结果解析：按真实载荷形态取值。

这个模块的存在理由是**一次真实的静默数据丢失**：``结果解析`` 节点（DSL 节点
``1774941932987``）原文按 ``operator_location_json`` / ``affiliated_company_json``
两个键取值，而 ``RelatedEnterpriseInfo`` 渠道的真实载荷从来不含它们——全部留痕实测
（5 个批次、8 份响应）里出现次数为 0。后果是两个字段恒为 ``{}``，企业信息分析 LLM
收到空输入，真实企业数据被丢弃，而运行整体仍是 ``succeeded``。

因此这里的用例不用构造的理想载荷，而用**留痕里的真实形状**：形态 A（天眼查，有企业
数据）与形态 B（无企业数据，只有归属地）。这是有意差异 W50 的回归防线。
"""

from __future__ import annotations

import json

from customer_profile.workflows.phone_company_analysis import (
    COMPANY_BLOCK_KEYS,
    has_content,
    split_operator_and_company,
)


# 形态 B：无关联企业。字段名与取值域照抄留痕（含 have_company_info 是字符串 "false"）
SHAPE_NO_COMPANY = {
    "company_main": None,
    "company_detail": None,
    "have_company_info": "false",
    "operator_location": {
        "location": "",
        "operator": "",
        "area_code": "",
        "card_type": "",
        "postal_code": "",
    },
}

# 形态 A：有企业数据（字段摘自留痕原文）
SHAPE_WITH_COMPANY = {
    "business_info": {
        "company_name": "某市某区某某水果店",
        "legal_representative": "某",
        "registration_status": "开业",
        "national_standard_industry": "果品、蔬菜零售(F5223)",
    },
    "shareholder_info": [],
    "enterprise_base_info": {
        "company_address": "某省某市某路",
        "company_profile": "某市某区某某水果店，成立于2020年",
        "email": "-",
    },
}


def test_operator_location_is_taken_from_the_real_key():
    """归属地取 ``operator_location``——形态 B 里的那个嵌套字典。"""
    result = split_operator_and_company(json.dumps(SHAPE_NO_COMPANY, ensure_ascii=False))
    assert json.loads(result["operator_location"]) == SHAPE_NO_COMPANY["operator_location"]


def test_company_blocks_are_collected_from_both_shapes():
    """企业内容按块收集，只收有实内容的块；空的骨架字段不得写进报告。"""
    result = split_operator_and_company(json.dumps(SHAPE_WITH_COMPANY, ensure_ascii=False))
    collected = json.loads(result["affiliated_company"])

    assert set(collected) == {"business_info", "enterprise_base_info"}
    assert collected["business_info"]["company_name"] == "某市某区某某水果店"
    # shareholder_info 是空列表，必须被剔除——否则报告里会出现一个无意义的空字段
    assert "shareholder_info" not in collected


def test_no_company_payload_yields_empty_company_block():
    """形态 B 的企业块必须是空对象，让下游 LLM 明确输出「无关联企业」。

    ``have_company_info`` 是字符串 ``"false"``、``company_main`` 是 ``None``：照「键存在
    即算有数据」的写法会把它们塞进企业块，LLM 于是收到一堆空字段却以为有数据。
    """
    result = split_operator_and_company(json.dumps(SHAPE_NO_COMPANY, ensure_ascii=False))
    assert json.loads(result["affiliated_company"]) == {}


def test_dsl_original_keys_still_work():
    """兼容读取：若上游日后真的传来 DSL 原来的键名，仍须能取到。"""
    payload = {
        "operator_location_json": {"operator": "中国移动"},
        "affiliated_company_json": {"company_name": "某某公司"},
    }
    result = split_operator_and_company(json.dumps(payload, ensure_ascii=False))
    assert json.loads(result["operator_location"]) == {"operator": "中国移动"}
    assert json.loads(result["affiliated_company"]) == {"company_name": "某某公司"}


def test_empty_and_none_body_do_not_raise():
    """空串与 ``None`` 都按空对象处理——原文的兜底行为，不得因为改写而丢失。"""
    for body in ("", None, {}):
        result = split_operator_and_company(body)
        assert result == {"operator_location": "{}", "affiliated_company": "{}"}


def test_non_json_string_raises_like_the_original():
    """非 JSON 字符串仍按原文抛异常，不静默吞掉——原文没有兜底，这里也不加。"""
    import pytest

    with pytest.raises(json.JSONDecodeError):
        split_operator_and_company("这不是 JSON")


def test_unsupported_body_type_raises():
    import pytest

    with pytest.raises(ValueError):
        split_operator_and_company(12345)


def test_has_content_rejects_the_placeholder_values():
    """``has_content`` 的判据：``None`` / 空串 / 空容器 / ``"false"`` / ``"null"`` 都算空。"""
    for value in (None, "", "   ", "false", "FALSE", "null", "none", [], {}, ()):
        assert not has_content(value), f"{value!r} 应判为空"
    for value in ("x", "-", 0, 1, {"a": 1}, [1], True):
        assert has_content(value), f"{value!r} 应判为有内容"


def test_company_block_keys_cover_both_observed_shapes():
    """块名表必须同时覆盖实测的两种载荷形态，否则改一处会静默漏另一处。"""
    assert {"business_info", "enterprise_base_info", "shareholder_info"} <= set(COMPANY_BLOCK_KEYS)
    assert {"company_main", "company_detail"} <= set(COMPANY_BLOCK_KEYS)

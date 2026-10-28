"""参考标准表：字段覆盖、准入模式合法性与规则归属。

这一层最容易出错的不是数据本身，而是**覆盖面**：新增一个回传字段却忘了登记准入模式，
页面上就会少一行，而且没有任何报错。这里钉住的就是这件事——表与 ``EXTRACTION_KEYS``
必须一一对应。
"""

from __future__ import annotations

import pytest

from customer_profile import internal_fields as inf
from customer_profile import notes_contract as nc
from customer_profile import reference_standards as rs


def test_standards_cover_every_extraction_field():
    """应管字段 = 取证全集减去画像卡字段；缺一行或多一行都要报出来。"""
    assert rs.verify() == []

    expected = {key for key in inf.EXTRACTION_KEYS if key not in inf.CARD_ONLY_KEYS}
    actual = {item.attribute_key for item in rs.FIELDS}
    assert actual == expected
    assert len(rs.FIELDS) == len(expected)


def test_standards_exclude_deal_level():
    """画像卡字段 ``deal_level`` 按约定不纳入，产出逻辑复杂，留待后续。"""
    assert rs.get("deal_level") is None


def test_standards_codes_are_stable_and_unique():
    """编号一经发出不复用：形态是 ``F`` + 两位数字，且互不重复。"""
    codes = [item.code for item in rs.FIELDS]
    assert len(set(codes)) == len(codes)
    assert all(code.startswith("F") and code[1:].isdigit() for code in codes)
    assert codes[0] == "F01"


def test_admission_mode_split_matches_business_decision():
    """事实 32 / 受控推断 28 是 2026-10-28 与业务方逐字段确认的结果。

    这条断言的作用不是「数字好看」，而是**有人改了一个字段的准入模式时必须同时改这里**，
    否则改动会静默生效。要调整请连同本条一起改，并在提交信息里写明是哪个字段。
    """
    from collections import Counter

    counts = Counter(item.admission for item in rs.FIELDS)
    assert counts[rs.ADMISSION_FACT] == 32
    assert counts[rs.ADMISSION_INFERRED] == 28


@pytest.mark.parametrize(
    "attribute_key",
    ["family_structure", "purchase_type", "payment_method", "license_plate_status", "decision_maker"],
)
def test_business_corrected_fields_are_inferred(attribute_key):
    """用户 2026-10-28 改判的五个字段：这些值靠客户直说拿不到，必须允许归纳。"""
    assert rs.admission_of(attribute_key) == rs.ADMISSION_INFERRED


def test_admission_rejects_unknown_mode():
    with pytest.raises(ValueError):
        rs.FieldStandard(code="F99", attribute_key="x", admission="free_guess")


def test_row_payload_carries_derived_columns():
    """派生列必须现读契约：名称、业务类型、编外标注都不能是手写副本。"""
    rows = {row["attribute_key"]: row for row in rs.build_rows()}

    name_row = rows["customer_name"]
    assert name_row["name"] == nc.by_key("customer_name")["attribute_name"]
    assert name_row["category"] == nc.category_of("customer_name")
    assert name_row["scope_label"] == "回传 Notes"

    # 编外字段：分类回落、标注「不回传」，输出模式按枚举定义渲染
    internal = rows["purchase_nature"]
    assert internal["scope_label"] == "编外（不回传）"
    assert internal["category"] == "编外字段"
    assert "单选" in internal["output_mode"]


def test_field_bound_rules_are_single_field_prompt_rules():
    """规则归属判据：单字段 + 提示词通道。多字段规则必须留在规则清单。"""
    from customer_profile.rules import load

    registry = load()
    bound = {rule.id for rule in registry.all() if rs.is_field_bound(rule)}

    assert "SPEC-02" in bound
    assert "SPEC-16" in bound  # 用户确认归表：单字段，同时挂 code 与 prompt
    assert "SPEC-17" not in bound  # 五个字段一条口径，塞进任何一行都只代表一个
    assert "SPEC-18" in bound  # 只挂 viewing_progress
    assert "HARD-01" not in bound  # 全局规则
    assert "EXPR-01" not in bound


def test_rules_endpoint_marks_field_bound_rows():
    """``/rules`` 仍返回全量，只多一个标记；过滤是页面的事。"""
    from customer_profile.rules import load

    registry = load()
    for rule in registry.all():
        marked = rs.is_field_bound(rule)
        assert marked == (len([s for s in rule.scope if s != "*"]) == 1 and "prompt" in rule.channels)

"""收入簇。

两条规则的分工是这个簇设计的全部要点：

- ``INCOME-02`` 是**形态**问题——车价区间不是收入，整条拒收；
- ``INCOME-03`` 是**口径**问题——第三方档位要换成金额区间，且换算只做一次。

``INCOME-03`` 只登记口径，换算实现仍在 :mod:`customer_profile.income_band`：取数节点
也调它，规则层不接管调用方（设计 §10「规则层只保留口径定义，换算是调用方」）。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="INCOME-02",
        version=1,
        title="收入字段只认收入标签",
        content_kind="hard",
        channels=("code",),
        scope=("annual_income",),
        stages=("project",),
        severity="block",
        default_action="reject",
        message_internal="收入断言由「车型价格/手机价格」类标签支撑：整条拒收，不降级",
        message_customer="收入信息需向客户或销售确认",
        owner="engineering",
        status="active",
        cluster="income",
        tests=(
            "tests/test_income_band.py::test_car_price_tag_is_not_written_as_income",
            "tests/test_income_band.py::test_untagged_prediction_amount_passes_through",
        ),
        legacy_codes=("e_non_income_tag",),
    ),
    Rule(
        id="INCOME-01",
        version=1,
        title="职业与收入量级不自洽",
        content_kind="consistency",
        channels=("llm_checker",),
        # 两个字段都参与判断：只给收入或只给职业都判不了。
        scope=("annual_income", "occupation"),
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal=(
            "职业描述与收入量级明显不匹配（如「网约车司机」配「30-50 万」）："
            "把收入降为推断档，不改取值"
        ),
        message_customer="",
        owner="business",
        status="active",
        cluster="income",
        tests=(
            "tests/rules/test_checker.py::test_income_01_fires_on_mismatched_pair",
            "tests/rules/test_checker.py::test_income_01_silent_on_consistent_pair",
        ),
    ),
    Rule(
        id="INCOME-03",
        version=1,
        title="第三方收入档位换成金额区间，只换算一次",
        content_kind="spec",
        channels=("code",),
        scope=("annual_income",),
        stages=("extract", "project"),
        severity="repair",
        default_action="rewrite",
        message_internal="1–6 档 → 业务专家确认的金额区间；客户自述与模型推断的金额原样透传",
        message_customer="",
        owner="both",
        status="active",
        cluster="income",
        tests=(
            "tests/test_income_band.py::test_income_band_ranges_match_business_spec",
            "tests/test_income_band.py::test_normalize_income_pure_function",
        ),
        legacy_codes=("income_band",),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

INCOME_TAG_KEYWORD = "收入"
"""收入标签名的判定词：「预测收入水平」含它，「预测车型价格偏好」「预测手机价格」不含。"""

PREDICTION_TIER = "prediction"
"""第三方预测档的字面量。与 ``projection.PREDICTION`` 同值，测试钉住。"""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value)


def is_non_income_tag(claim: Any) -> bool:
    """这条收入断言是否由**非收入**的第三方标签支撑。

    只判第三方预测档：一手交互与登记来源的收入表述不在此列（那可能是客户自述或
    销售确认，不是标签串字段的问题）。

    判定看取值、标签名（``quote``）、当归原文与推断依据——L1 按模板要求把标签名抄进
    ``quote`` 与 ``basis_of_inference``，旧产物至少有一处。三处都不含「收入」即判非收入。

    只拦**看得出出自标签**的断言：有标签名却没有「收入」。没有任何标签痕迹的，是模型
    基于别的事实自行推断的金额（如「职业为网约车司机，推断 20-40 万」），原样放行。
    """
    if getattr(claim, "tier", "") != PREDICTION_TIER:
        return False
    parts = [
        _text(claim.value),
        claim.quote or "",
        claim.source_quote or "",
        *(claim.basis_of_inference or ()),
    ]
    text = " ".join(parts)
    tagged = bool(claim.quote or claim.source_quote) or "标签" in text
    if not tagged:
        return False
    return INCOME_TAG_KEYWORD not in text


def check_income_tag(
    attribute: str, group: Sequence[Any]
) -> tuple[list[Any], list[Action]]:
    """e 段：收入字段只认收入标签。

    返回 ``(留下的断言, 动作)``。全部被拒时动作里已含逐条留痕，调用方据此补一条
    ``to_confirm``——「转人工」这一步仍然属于投影层的装配职责，规则只负责判定谁不合法。
    """
    if attribute != "annual_income":
        return list(group), []
    allowed = [claim for claim in group if not is_non_income_tag(claim)]
    dropped = [claim for claim in group if is_non_income_tag(claim)]
    actions = [
        Action(
            rule="INCOME-02",
            attribute=attribute,
            action="reject",
            severity="block",
            reason="取值来自「车型价格 / 手机价格」等非收入标签，描述的不是年收入",
            remove=False,
            payload={
                "attribute": attribute,
                "value": claim.value,
                "rule": "e_non_income_tag",
                "reason": "取值来自「车型价格 / 手机价格」等非收入标签，描述的不是年收入",
            },
        )
        for claim in dropped
    ]
    return allowed, actions

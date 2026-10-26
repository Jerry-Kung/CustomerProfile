"""跨字段一致性簇：两处说法不能同时成立。

这一簇的规则都改 ``facts`` 本身（不是降级），因为矛盾的一方**必须被撤销**——保留
一个无凭据的「换购」比留白更糟，它会被下游当成已确认事实喂给表达层。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="CONSIST-01",
        version=1,
        title="有旧车 ⇔ 换购",
        content_kind="consistency",
        channels=("code",),
        scope=("purchase_nature", "old_car_model", "trade_in_requirement"),
        stages=("project",),
        severity="repair",
        default_action="rewrite",
        message_internal="旧车或置换意愿任一成立即定换购；声称换购却无凭据则撤销并转确认",
        message_customer="确认客户是否有旧车置换",
        owner="engineering",
        status="active",
        cluster="consistency",
        tests=(
            "tests/test_projection.py::test_trade_in_yes_implies_replacement",
            "tests/test_projection.py::test_replacement_without_evidence_is_withdrawn",
        ),
        legacy_codes=("f_trade_in",),
    ),
    Rule(
        id="CONSIST-03",
        version=1,
        title="已注销企业不得当作在职企业",
        content_kind="hard",
        channels=("code",),
        scope=("company", "company_type"),
        stages=("project",),
        severity="block",
        default_action="reject",
        message_internal="关联企业登记状态为注销：company / company_type 整条拒收",
        message_customer="",
        owner="engineering",
        status="active",
        cluster="consistency",
        tests=(
            "tests/test_projection.py::test_deregistered_company_is_not_filled",
            "tests/rules/test_landings.py::test_company_kept_when_not_deregistered",
        ),
        legacy_codes=("f_deregistered",),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

NO_OLD_CAR_VALUES: frozenset[str] = frozenset({"无", "没有", "无旧车"})
"""旧车字段里表示「没有旧车」的写法。它们出现时**不构成**换购凭据。"""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value)


def _trade_in_yes(value: Any) -> bool:
    if value == "yes":
        return True
    return isinstance(value, dict) and value.get("key") == "yes"


def check_cross_field(
    facts: Mapping[str, Any], context: Mapping[str, Any]
) -> list[Action]:
    """f 段：跨字段一致性。

    两条规则都在**改写 ``facts`` 本身**，因为矛盾的一方必须被撤销——保留一个无凭据的
    「换购」比留白更糟，它会被下游当成已确认事实喂给表达层。

    ``context`` 承载原文里的旁证（是否出现旧车证据、企业是否注销），它们不是字段值，
    因此无法从 ``facts`` 推出，只能由调用方传入。
    """
    actions: list[Action] = []

    # —— CONSIST-01：旧车 ⇔ 换购
    old_car = _text(facts.get("old_car_model"))
    has_old_car = bool(old_car) and old_car not in NO_OLD_CAR_VALUES
    has_old_car = has_old_car or bool(context.get("has_old_car_evidence"))
    # ``trade_in_requirement=yes`` **本身就是**「有旧车」的凭据，与旧车证据同权。
    # 这里**不**清除 ``trade_in_requirement``——清除它会抹掉置换的唯一凭据。
    trade_in_yes = _trade_in_yes(facts.get("trade_in_requirement"))
    if (has_old_car or trade_in_yes) and facts.get("purchase_nature") != "replacement":
        actions.append(
            Action(
                rule="CONSIST-01",
                attribute="purchase_nature",
                action="rewrite",
                severity="repair",
                reason="有旧车证据或置换意愿，购车性质定为换购",
                value="replacement",
                # 这是**确定性改写**：证据就在 facts 里，改完仍是事实，不降档。
                affects_basis=False,
            )
        )
    if not (has_old_car or trade_in_yes) and facts.get("purchase_nature") == "replacement":
        actions.append(
            Action(
                rule="CONSIST-01",
                attribute="purchase_nature",
                action="reject",
                severity="repair",
                reason="声称换购但无旧车证据支撑",
                payload={
                    "attribute": "purchase_nature",
                    "value": "replacement",
                    "rule": "f_trade_in",
                    "reason": "声称换购但无旧车证据支撑",
                },
            )
        )
        actions.append(
            Action(
                rule="CONSIST-01",
                attribute="purchase_nature",
                action="require_confirm",
                severity="repair",
                reason="换购与其证据不一致",
                customer_reason="确认客户是否有旧车置换",
                payload={
                    "attribute_key": "purchase_nature",
                    "reason": "换购与其证据不一致",
                    "suggested_action": "确认客户是否有旧车置换",
                },
            )
        )

    # —— CONSIST-03：已注销企业不得当作在职企业
    if context.get("company_deregistered"):
        for key in ("company", "company_type"):
            if key not in facts:
                continue
            actions.append(
                Action(
                    rule="CONSIST-03",
                    attribute=key,
                    action="reject",
                    severity="block",
                    reason="关联企业登记状态为注销，不得当作在职企业",
                    payload={
                        "attribute": key,
                        "value": facts[key],
                        "rule": "f_deregistered",
                        "reason": "关联企业登记状态为注销，不得当作在职企业",
                    },
                )
            )
    return actions

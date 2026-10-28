"""商机等级簇。

三条规则对应业务专家《客户商机等级定级规范》里**可机器判定**的部分；其余靠定级
提示词。这里的规则全部是「把模型的结论拉回规范」——模型偶发越界是常态，不是异常，
所以每条都是强制收口而不是提示。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="LEVEL-01",
        version=1,
        title="卡片与商机两处等级不一致时唯一化",
        content_kind="consistency",
        channels=("code",),
        scope=("deal_level", "business_opp_level"),
        stages=("project",),
        severity="repair",
        default_action="rewrite",
        message_internal="两处等级不一致：取较高档写入 deal_level，并把两个候选转人工确认",
        message_customer="卡片与商机等级不一致，请确认最终等级",
        owner="engineering",
        status="active",
        cluster="level",
        tests=(
            "tests/test_projection.py::test_level_conflict_unified_to_one_value",
            "tests/rules/test_landings.py::test_single_level_is_not_touched",
        ),
        legacy_codes=("f_level",),
    ),
    Rule(
        id="LEVEL-02",
        version=1,
        title="线索邀约阶段最高 A",
        content_kind="hard",
        channels=("code",),
        scope=("deal_level",),
        stages=("gate",),
        severity="block",
        default_action="downgrade",
        message_internal="业务阶段为线索邀约：H 强制降为 A（阶段由代码判定，不由模型）",
        message_customer="",
        owner="business",
        status="active",
        cluster="level",
        tests=(
            "tests/test_level.py::test_lead_stage_caps_h_at_a",
            "tests/test_level.py::test_stage_is_opportunity_when_test_drive_recording_exists",
        ),
        legacy_codes=("R04",),
    ),
    Rule(
        id="LEVEL-03",
        version=1,
        title="H/A 必须同时具备需求、时间、行动三类证据",
        content_kind="hard",
        channels=("code",),
        scope=("deal_level",),
        stages=("gate",),
        severity="block",
        default_action="downgrade",
        message_internal="H/A 缺任一维度证据：降为 B",
        message_customer="",
        owner="business",
        status="active",
        cluster="level",
        tests=(
            "tests/test_level.py::test_h_without_multidimensional_evidence_downgrades_to_b",
            "tests/test_level.py::test_h_with_full_evidence_survives",
        ),
        legacy_codes=("R05",),
    ),
    Rule(
        id="LEVEL-04",
        version=1,
        title="信息不足是「待识别」，不是 C",
        content_kind="spec",
        channels=("code",),
        scope=("deal_level",),
        stages=("gate",),
        severity="warn",
        default_action="annotate",
        message_internal="缺证据不得判 C：规范里的「待识别」是独立结论，不是最低档",
        message_customer="",
        owner="business",
        status="active",
        cluster="level",
        tests=(
            "tests/test_level.py::test_apply_level_accepts_pending_identification",
            "tests/test_level.py::test_invalid_level_falls_back_to_l1_level",
        ),
        legacy_codes=("R08",),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

LEVEL_PENDING = "待识别"
"""规范里的「信息不足」结论。**不是 C 级**——不能因为缺证据就认定客户不想买。"""


def check_level_unify(
    facts: Mapping[str, Any],
    *,
    level_keys: Sequence[str],
    level_order: Sequence[str],
    normalize: Any,
) -> list[Action]:
    """project 阶段：把 ``deal_level`` 与 ``business_opp_level`` 收敛成一个值。

    ``normalize`` 由调用方传入（投影层已有的 ``normalize_level``）——等级归一本身是
    契约施工而不是业务规则，规则层只借它算「两处是不是同一个档」，不接管它的实现。
    """
    values: list[tuple[str, str]] = []
    for key in level_keys:
        if key not in facts:
            continue
        level = normalize(facts[key])
        if level:
            values.append((key, level))
    actions: list[Action] = []
    # 等级只在 ``deal_level`` 一处保留；其余载体一律清掉，不写留痕（它们是重复存储，
    # 不是「被规则否决的结论」，记成 rejected 会让核读清单里多出大量噪音）。
    # **清理与「能不能归一」无关**：归一不出来的脏键同样不能留在 facts 里——下游按
    # ``deal_level`` 读等级，多出来的键只会在回传时被当成并列结论。
    for key in level_keys:
        if key == "deal_level" or key not in facts:
            continue
        actions.append(
            Action(
                rule="LEVEL-01",
                attribute=key,
                action="reject",
                severity="repair",
                reason="等级只在 deal_level 一处保留",
                record=False,
            )
        )
    if not values:
        return actions

    distinct = {level for _, level in values}
    chosen = min(distinct, key=lambda lv: level_order.index(lv))
    if facts.get("deal_level") != chosen:
        actions.append(
            Action(
                rule="LEVEL-01",
                attribute="deal_level",
                action="rewrite",
                severity="repair",
                reason="两处等级不一致，取较高档",
                value=chosen,
                affects_basis=False,
            )
        )
    if len(distinct) > 1:
        actions.append(
            Action(
                rule="LEVEL-01",
                attribute="deal_level",
                action="reject",
                severity="repair",
                reason="两处等级不一致，取较高档并转人工确认",
                # 值已经落定为 ``chosen``，这里的 reject 只是把冲突记进留痕，不撤回字段。
                remove=False,
                payload={
                    "attribute": "deal_level",
                    "value": dict(values),
                    "rule": "f_level",
                    "reason": "两处等级不一致，取较高档并转人工确认",
                },
            )
        )
        actions.append(
            Action(
                rule="LEVEL-01",
                attribute="deal_level",
                action="require_confirm",
                severity="repair",
                reason="卡片与商机等级不一致",
                customer_reason="人工确认最终等级",
                payload={
                    "attribute_key": "deal_level",
                    "candidates": sorted(distinct, key=lambda lv: level_order.index(lv)),
                    "reason": "卡片与商机等级不一致",
                    "suggested_action": "人工确认最终等级",
                },
            )
        )
    return actions

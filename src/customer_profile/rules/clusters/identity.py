"""身份与归属簇：一条断言说的事，是不是「客户本人」。

这一簇的规则全部在 ``promotion.project_claims`` 的 a / b 段执行，且都是**整条拒收**
或**降级保留**两类极端：把销售顾问的信息当客户信息写出去，比字段空着更糟；而社交
自呈现不足以支撑确定性，但线索仍要给销售看。中间地带（「判不太准」）不在这簇。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ... import text_utils as tut
from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="IDENT-01",
        version=1,
        title="非客户本人的断言不得写入身份字段",
        content_kind="hard",
        channels=("code",),
        scope=("identity_cluster",),
        stages=("project",),
        severity="block",
        default_action="reject",
        message_internal="断言主体不是客户本人（配偶、家人、同事、销售），身份字段整条拒收",
        message_customer="",
        owner="engineering",
        status="active",
        cluster="identity",
        tests=(
            "tests/test_projection.py::test_attribution_filter_blocks_non_customer_identity",
            "tests/test_projection.py::test_attribution_filter_still_blocks_other_identity_fields",
        ),
        legacy_codes=("a_attribution",),
    ),
    Rule(
        id="IDENT-02",
        version=1,
        title="取值或锚点指向销售顾问的，不得写进客户字段",
        content_kind="hard",
        channels=("code",),
        scope=("identity_cluster",),
        stages=("project",),
        severity="block",
        default_action="reject",
        message_internal="取值、引文或当归原文出现销售顾问指向词，整条拒收；登记档豁免",
        message_customer="",
        owner="engineering",
        status="active",
        cluster="identity",
        tests=(
            "tests/test_projection.py::test_salesperson_value_rejected_from_customer_name",
            "tests/test_projection.py::test_salesperson_gate_ignores_basis_of_inference",
        ),
        legacy_codes=("a_salesperson_value",),
    ),
    Rule(
        id="IDENT-03",
        version=1,
        title="身份字段不得只由社交自呈现支撑",
        content_kind="consistency",
        channels=("code",),
        scope=("identity_cluster",),
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal="身份字段仅有社交自呈现来源：降为 inferred_low 写入并转人工确认",
        message_customer="这一项来自客户公开页面，建议向客户本人核实",
        owner="engineering",
        status="active",
        cluster="identity",
        tests=(
            "tests/test_projection.py::test_social_alone_downgrades_identity_field",
            "tests/test_projection.py::test_prediction_alone_supports_identity_as_inferred_high",
        ),
        legacy_codes=("b_social_only",),
    ),
    Rule(
        id="ATTE-01",
        version=1,
        title="态度与行为类字段须有一手依据",
        content_kind="consistency",
        channels=("code",),
        scope=("emotion", "knowledge_of_mengshi", "pain_points"),
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal="态度类字段只有社交/预测来源：降为 inferred_low 写入",
        message_customer="客户态度类判断依据不足，建议沟通中再确认",
        owner="engineering",
        status="active",
        cluster="identity",
        tests=(
            "tests/test_projection.py::test_attitude_field_without_firsthand_downgrades",
            "tests/test_projection.py::test_prediction_can_support_non_identity_field",
        ),
        legacy_codes=("b_attitude_weak_source",),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================


def check_attribution(
    claims: Sequence[Any],
    *,
    guard_keys: frozenset[str],
    subject_exempt: frozenset[str],
    registry_tier: str,
    salesperson_markers: Iterable[str],
) -> tuple[list[Any], list[Action]]:
    """a 段：归属过滤与值形态门。

    返回 ``(通过的断言, 动作)``——**这是唯一一个同时过滤断言与产出动作的检查函数**：
    归属问题要拦的是「这条断言」，而不是「这个字段的值」，所以不能靠改 ``facts`` 表达。

    参数由投影层传入（``IDENTITY_GUARD_KEYS`` 等），而不是在这里再抄一份：那些集合
    一半来自契约快照、一半来自编外字段登记表，抄一份就会在下次契约变动时漂移。
    """
    markers = tuple(salesperson_markers)
    kept: list[Any] = []
    actions: list[Action] = []
    for claim in claims:
        if not claim.attribute:
            continue
        guarded = claim.attribute in guard_keys
        if (
            guarded
            and claim.attribute not in subject_exempt
            and claim.subject != "customer"
        ):
            actions.append(
                Action(
                    rule="IDENT-01",
                    attribute=claim.attribute,
                    action="reject",
                    severity="block",
                    reason=f"事实主体是 {claim.subject}，不是客户本人",
                    remove=False,
                    payload={
                        "attribute": claim.attribute,
                        "value": claim.value,
                        "rule": "a_attribution",
                        "reason": f"事实主体是 {claim.subject}，不是客户本人",
                    },
                )
            )
            continue
        if (
            guarded
            and claim.tier != registry_tier
            and _salesperson_marked(claim, markers)
        ):
            actions.append(
                Action(
                    rule="IDENT-02",
                    attribute=claim.attribute,
                    action="reject",
                    severity="block",
                    reason="取值/锚点指向我方销售顾问，不是客户本人信息",
                    remove=False,
                    payload={
                        "attribute": claim.attribute,
                        "value": claim.value,
                        "rule": "a_salesperson_value",
                        "reason": "取值/锚点指向我方销售顾问，不是客户本人信息",
                    },
                )
            )
            continue
        kept.append(claim)
    return kept, actions


def _salesperson_marked(claim: Any, markers: tuple[str, ...]) -> bool:
    """取值、引文或当归原文里出现销售顾问指向词。

    **不看** ``basis_of_inference``——那是推断依据，本就常提到「销售说…」，把它算进来
    会误杀大量正确的推断（实测 ``title`` 的依据多为「销售以『大哥』称呼对方」）。
    """
    text = " ".join(
        (tut.as_text(claim.value), claim.quote or "", claim.source_quote or "")
    )
    return any(marker in text for marker in markers)


def check_weak_source(
    attribute: str,
    group: Sequence[Any],
    *,
    guard_keys: frozenset[str],
    weak_tiers: frozenset[str],
    attitude_keys: frozenset[str],
    social_wechat_exempt: str = "wechat_id",
) -> list[Action]:
    """b 段：身份字段的社交自呈现降级，与态度类字段的一手依据要求。

    第三方预测单独支撑身份字段的那条分支（改记 ``inferred_high``）**不在这里**：
    它是相反方向的政策，等 M4 作为 ``IDENT-04`` 单独登记，免得一条规则同时表达
    「降级」与「升级」两种意图。
    """
    actions: list[Action] = []
    weak_only = all(claim.tier in weak_tiers for claim in group)
    values = [claim.value for claim in group]
    if (
        attribute in guard_keys
        and weak_only
        and attribute != social_wechat_exempt
        and not all(claim.is_prediction for claim in group)
    ):
        actions.append(
            Action(
                rule="IDENT-03",
                attribute=attribute,
                action="downgrade",
                severity="repair",
                reason="身份字段仅由社交自呈现支撑",
                payload={
                    "attribute": attribute,
                    "value": values,
                    "rule": "b_social_only",
                    "reason": "身份字段仅由社交自呈现支撑",
                    "action": "降为 inferred_low 写入（不丢字段）",
                },
            )
        )
    if attribute in attitude_keys and all(
        claim.tier in weak_tiers for claim in group
    ):
        actions.append(
            Action(
                rule="ATTE-01",
                attribute=attribute,
                action="downgrade",
                severity="repair",
                reason="态度类字段仅有社交/预测依据，无一手或登记来源",
                payload={
                    "attribute": attribute,
                    "value": values,
                    "rule": "b_attitude_weak_source",
                    "reason": "态度类字段仅有社交/预测依据，无一手或登记来源",
                    "action": "降为 inferred_low 写入（不丢字段）",
                },
            )
        )
    return actions

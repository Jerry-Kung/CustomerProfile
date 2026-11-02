"""跨字段一致性簇：两处说法不能同时成立。

这一簇的规则都改 ``facts`` 本身（不是降级），因为矛盾的一方**必须被撤销**——保留
一个无凭据的「换购」比留白更糟，它会被下游当成已确认事实喂给表达层。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

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
    Rule(
        id="CONSIST-05",
        version=1,
        title="有置换凭据时不得是无车",
        content_kind="consistency",
        channels=("code",),
        scope=("current_vehicle_info",),
        stages=("project",),
        severity="repair",
        default_action="rewrite",
        message_internal=(
            "有旧车 / 拥车 / 置换凭据，现车信息却是「无车」：改写为有车（车型车龄未知），"
            "降档保留，证据一并改指该凭据"
        ),
        message_customer="客户名下有可置换的旧车，现车情况请核实具体车型与车龄",
        owner="business",
        status="active",
        cluster="consistency",
        tests=(
            "tests/test_projection.py::test_no_car_rewritten_when_trade_in_evidence_exists",
            "tests/rules/test_landings.py::test_no_car_kept_without_evidence",
        ),
    ),
    Rule(
        id="CONSIST-04",
        version=1,
        title="小订/大定不是拥车",
        content_kind="consistency",
        channels=("code",),
        scope=("ownership_cluster",),
        stages=("project",),
        severity="block",
        default_action="reject",
        message_internal="断言原文或推断依据含小订/大定一类下单动作表述，却落到拥车/置换类字段：断言整条拒收",
        message_customer="",
        owner="business",
        status="active",
        cluster="consistency",
        tests=(
            "tests/test_projection.py::test_deposit_claim_rejected_from_ownership_fields",
            "tests/rules/test_landings.py::test_owned_car_claim_kept_when_not_a_deposit",
        ),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

NO_OLD_CAR_VALUES: frozenset[str] = frozenset({"无", "没有", "无旧车"})
"""旧车字段里表示「没有旧车」的写法。它们出现时**不构成**换购凭据。"""

DEPOSIT_PHRASES: tuple[str, ...] = (
    "小订",
    "小定",
    "大订",
    "大定",
    "下订",
    "下定",
    "定金",
    "意向金",
    "锁单",
)
"""下单动作词：出现即说明说的是「在购车阶段交钱了」，不是「在开 / 持有这台车」。

词表是**封闭**的（业务方 2026-10-28 定稿：先用这一版，后续遇到新说法再加）。它只用于
「拥车与置换类字段」的断言，不看别的字段——``intended_model`` 里出现小订是正确形态。
"""

NO_CAR_MARKERS: tuple[str, ...] = ("无车", "没有车", "暂无车", "名下无车")
"""「无车」类取值。命中且另有置换凭据时，两处互相矛盾，必须改写。"""

NO_CAR_REWRITE = "有车（具体车型与车龄待核实）"
"""跨字段改写的落定值。

刻意不写具体车型：凭据只说「有旧车 / 要置换」，没说车型与车龄。凭现有信息补出来的
车型就是编造——销售拿着一个猜的车名去压价，比留白更糟。
"""

OWNERSHIP_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "old_car_model",
        "owned_brand_model",
        "owned_car_age",
        "current_vehicle_info",
        "trade_in_model",
        "trade_in_requirement",
        "purchase_nature",
    }
)
"""「客户本来就有这台车」的语义字段。小订/大定对应的车型不得落到这里。

``purchase_nature`` 在内：模型会由「小订车」推出「要置换」（实测 19247201510 即如此），
它是同一句误读的下游，必须一并拦。
"""


def check_deposit_claims(
    claims: Sequence[Any],
    *,
    ownership_keys: frozenset[str] = OWNERSHIP_ATTRIBUTES,
    deposit_phrases: tuple[str, ...] = DEPOSIT_PHRASES,
) -> tuple[list[Any], list[Action]]:
    """a 段（CONSIST-04）：小订/大定的车型不得当作拥车或置换证据。

    与 :func:`check_cross_field` 的分工：那条看的是**已落定的 facts**，这条看的是
    **断言本身**——错在「这句话被读成了什么」，把它放进 facts 再拦已经晚了三步。
    实测 ``19247201510``：原文只有「x700小订客户」，L1 给出 ``old_car_model=x700``
    （锚点「x700小订客户」），L1-1c 又据此推出「有车」「要置换」；落进 facts 之后
    CONSIST-01 会顺势把购车性质改成换购，越拦越实。

    判据看三处：取值、引文（锚点 / 当归原文）、推断依据。第三处不能省——
    ``19247201510`` 的 ``current_vehicle_info`` 没有锚点，唯一露出「小订」的地方
    就是它的推断依据「客户为x700小订客户」。

    返回 ``(通过的断言, 动作)``，与 :func:`identity_rules.check_attribution` 同形。
    """
    kept: list[Any] = []
    actions: list[Action] = []
    for claim in claims:
        if claim.attribute not in ownership_keys or not _deposit_marked(
            claim, deposit_phrases
        ):
            kept.append(claim)
            continue
        actions.append(
            Action(
                rule="CONSIST-04",
                attribute=claim.attribute,
                action="reject",
                severity="block",
                reason="小订/大定是下单动作，不是拥车或置换证据",
                remove=False,
                payload={
                    "attribute": claim.attribute,
                    "value": claim.value,
                    "rule": "f_deposit",
                    "reason": "小订/大定是下单动作，不是拥车或置换证据",
                },
            )
        )
    return kept, actions


def _deposit_marked(claim: Any, phrases: tuple[str, ...]) -> bool:
    """取值、引文或推断依据里出现下单动作词。"""
    parts: list[str] = [_text(claim.value), claim.quote or "", claim.source_quote or ""]
    parts.extend(str(item) for item in (claim.basis_of_inference or ()))
    text = " ".join(parts)
    return any(phrase in text for phrase in phrases)


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


def _is_no_car_value(value: Any) -> bool:
    """现车信息是不是「无车」类取值（文本或结构化串都认）。"""
    text = _text(value)
    if not text:
        return False
    for item in _value_fragments(text):
        if any(marker in item for marker in NO_CAR_MARKERS):
            return True
    return any(marker in text for marker in NO_CAR_MARKERS)


def _value_fragments(text: str) -> list[str]:
    """把一个取值拆成可比对的片段（结构化串取 ``key``，多选按 ``||`` 拆）。"""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return [stripped]
        if isinstance(parsed, dict):
            return [
                str(parsed.get("key") or "").strip(),
                str(parsed.get("input_value") or "").strip(),
            ]
    if "||" in stripped:
        return [part.strip() for part in stripped.split("||") if part.strip()]
    return [stripped]


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

    # —— CONSIST-05：有置换凭据时不得是「无车」
    # 业务方报障的原始矛盾：同一张卡上「现车信息=无车」与「置换车型=某旧车车型」并存。
    # 一手置换证据压过第三方「无车」标签，因此**改值**；但落定值不编造具体车型，
    # 并降档保留（前端已有低置信标记）——依据是「另一处有凭据」，不是客户原话直接
    # 给出本字段取值，所以不能继续声称事实。
    no_car = facts.get("current_vehicle_info")
    if _is_no_car_value(no_car) and (has_old_car or trade_in_yes):
        actions.append(
            Action(
                rule="CONSIST-05",
                attribute="current_vehicle_info",
                action="rewrite",
                severity="repair",
                reason="有旧车或置换凭据，现车信息不可能是无车",
                customer_reason="客户名下有可置换的旧车，现车情况请核实具体车型与车龄",
                value=NO_CAR_REWRITE,
                affects_basis=True,
                payload={
                    "attribute": "current_vehicle_info",
                    "value": no_car,
                    "rule": "f_no_car_conflict",
                    "reason": "有旧车或置换凭据，现车信息不可能是无车",
                },
            )
        )
    return actions

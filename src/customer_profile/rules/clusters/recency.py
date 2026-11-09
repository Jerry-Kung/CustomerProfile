"""时近性规则簇：新来的证据能不能翻掉上一版的结论。

三条规则对应 ``01`` §6.2 里「一条新规则簇」的全部内容。两条是**字段级**取舍
（``RECENT-01`` / ``RECENT-02``），一条是**定级守卫**（``RECENT-03``）。

为什么这一簇单独存在、而不是并进投影层：投影层只看得到本轮断言，看不到底座——
「上一版已经定了什么」不在它的视野里。时近性的输入是「底座断言 vs 新断言」这一对，
因此执行点在合并账本（:mod:`customer_profile.incremental.merge`）与定级收口。

三条规则都可单条关闭：``RULES_DISABLED=RECENT-02``。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..registry import Rule

STEADY_FIELDS: tuple[str, ...] = ()
"""留空：稳态字段由 :data:`PROCESS_FIELDS` 的补集动态算出（见 :func:`is_steady`）。"""

PROCESS_FIELDS: tuple[str, ...] = (
    # ---- 购车需求
    "intended_model",
    "budget_range",
    "planned_purchase_time",
    "viewing_progress",
    "purchase_purpose",
    "core_concerns",
    "optional_config",
    # ---- 决策与节奏
    "follow_up_frequency",
    "decision_maker",
    "knowledge_of_mengshi",
    # ---- 态度
    "emotion",
    "pain_points",
    "brand_concerns",
    "competing_brand",
    "competing_model",
    # ---- 置换与金融
    "trade_in_requirement",
    "trade_in_model",
    "payment_method",
)
"""**进程字段**：随销售流程推进而变化的字段（``05`` Q2 首版清单，按建议采纳）。

这些字段的正确答案就是「后到的更准」：客户这周说预算 20 万，上周说 18 万，该用 20 万。
其余字段（基础身份 16 项、``hobbies`` / ``life_key_nodes`` / ``family_structure`` 等）
是**稳态字段**：年龄、籍贯、婚否不因为一次新的弱信号而变。
"""

RULES: tuple[Rule, ...] = (
    Rule(
        id="RECENT-01",
        version=1,
        title="稳态字段：新弱信号不翻旧强事实",
        content_kind="consistency",
        channels=("code",),
        scope=("*",),
        stages=("project",),
        severity="repair",
        default_action="reject",
        message_internal=(
            "稳态字段（身份、家庭、籍贯一类）上，本轮只有弱信号或低置信推断时，"
            "沿用上一版已定稿结论；新断言退场并留痕。"
        ),
        message_customer="",
        owner="engineering",
        status="active",
        cluster="recency",
        tests=(
            "tests/rules/test_recency.py::test_steady_field_keeps_previous_conclusion",
            "tests/rules/test_recency.py::test_steady_field_takes_new_fact",
        ),
    ),
    Rule(
        id="RECENT-02",
        version=1,
        title="进程字段：同档后到者胜，弱档须带推翻标注",
        content_kind="consistency",
        channels=("code",),
        scope=PROCESS_FIELDS,
        stages=("project",),
        severity="repair",
        default_action="rewrite",
        message_internal=(
            "进程字段（预算、意向车型、进度、时间、态度等）上，新断言与上一版同档或更硬时"
            "以后到者为准；低一档但带「推翻上一版」标注时降置信保留并转人工确认。"
        ),
        message_customer="客户近期表述与此前记录不同，请与客户确认最新情况",
        owner="business",
        status="active",
        cluster="recency",
        tests=(
            "tests/rules/test_recency.py::test_process_field_later_one_wins",
            "tests/rules/test_recency.py::test_process_field_weaker_signal_keeps_base",
        ),
    ),
    Rule(
        id="RECENT-03",
        version=1,
        title="等级变化必须锚定本轮新增记录",
        content_kind="hard",
        channels=("code",),
        scope=("deal_level",),
        stages=("gate",),
        severity="block",
        default_action="downgrade",
        message_internal=(
            "增量运行中，商机等级相对上一版发生变化、却找不到本轮新增记录里的支撑时，"
            "回退上一版等级并留痕。阶段只能前进不能后退。"
        ),
        message_customer="",
        owner="business",
        status="active",
        cluster="recency",
        tests=(
            "tests/rules/test_recency.py::test_level_change_without_new_evidence_reverts",
            "tests/rules/test_recency.py::test_level_change_with_new_evidence_survives",
        ),
    ),
)


def is_process(attribute: str) -> bool:
    return attribute in PROCESS_FIELDS


def is_steady(attribute: str, contract_keys: Iterable[str]) -> bool:
    """稳态 = 契约内、且不是进程字段。契约外的字段（编外推断）一律按稳态处理——
    它们多是身份类（``company`` / ``old_car_model`` / ``emotion`` 除外已列入进程）。"""
    return attribute not in PROCESS_FIELDS


# ---------------------------------------------------------------- 判据
BASIS_RANK: dict[str, int] = {"fact": 0, "inferred_high": 1, "inferred_low": 2}


def _rank(basis: Any) -> int:
    return BASIS_RANK.get(str(basis or "").strip(), 2)


def _supersedes(claim: dict) -> bool:
    """新断言是否明确标注「本条推翻上一版」。

    L1 若按提示词给了 ``supersedes``（字段名或 ``true``），或 ``claim_type`` 为
    ``overturn``，都算标注。**模板未加标注段之前这个分支不会命中**——那时等价于
    「弱信号不翻旧结论」，与本规则的设计取向一致（宁可不翻，不可乱翻）。
    """
    flag = claim.get("supersedes")
    if flag in (True, "true", "True"):
        return True
    return str(claim.get("claim_type") or "").strip() == "overturn"


def decide_field(
    *,
    attribute: str,
    base_claim: dict | None,
    new_claims: Sequence[dict],
    disabled: Iterable[str] = (),
) -> dict[str, Any]:
    """一个字段上「底座 vs 新断言」的取舍。返回 ``{"claims", "trace"}``。

    ``claims`` 是要进入合并账本的断言列表（已按判据筛过），``trace`` 是规则留痕条目
    （形状与 ``rule_trace_json`` 一致，进运行台）。
    """
    off = {str(item).strip() for item in disabled}
    new_claims = [c for c in new_claims if isinstance(c, dict)]
    if base_claim is None or not new_claims:
        return {"claims": list(new_claims) or ([base_claim] if base_claim else []), "trace": []}

    base_rank = _rank(base_claim.get("basis"))
    best_new = min(new_claims, key=lambda c: _rank(c.get("basis")))
    new_rank = _rank(best_new.get("basis"))
    trace: list[dict] = []

    if not is_process(attribute):
        # RECENT-01：稳态字段只认「更强或同强」的新断言。
        # 规则关闭时**两方都进账本**，由投影层按来源档裁决——那是关闭前的行为。
        if "RECENT-01" in off:
            return {"claims": new_claims + [base_claim], "trace": []}
        if new_rank <= base_rank:
            # 新断言更强：**底座必须退出**。底座的来源名是「上一版画像」，登记档，
            # 留在账本里会在投影层按来源档压过新事实——那正好把本规则的目的倒过来。
            return {"claims": new_claims, "trace": []}
        trace.append(
            {
                "rule_id": "RECENT-01",
                "stage": "project",
                "attribute": attribute,
                "action": "reject",
                "severity": "repair",
                "reason": "稳态字段：本轮只有弱信号，沿用上一版已定稿结论",
                "before": base_claim.get("value"),
                "after": [c.get("value") for c in new_claims],
                "source": "code",
                "version": 1,
            }
        )
        return {"claims": [base_claim], "trace": trace}

    # RECENT-02：进程字段同档或更硬即后到者胜。
    if "RECENT-02" in off:
        return {"claims": new_claims + [base_claim], "trace": []}
    if new_rank <= base_rank:
        # 同档/更硬 → 后到者胜，底座退出（理由同 RECENT-01：底座是登记档，留着会压过新结论）。
        return {"claims": new_claims, "trace": []}

    if _supersedes(best_new):
        # 弱一档但明确标注推翻：降置信保留，并交人工确认（动作由投影层的 to_confirm 承接）。
        for claim in new_claims:
            claim.setdefault("basis", "inferred_low")
            claim["overturns_base"] = True
        trace.append(
            {
                "rule_id": "RECENT-02",
                "stage": "project",
                "attribute": attribute,
                "action": "require_confirm",
                "severity": "repair",
                "reason": "新表述较弱但明确标注推翻上一版：降置信保留并转人工确认",
                "before": base_claim.get("value"),
                "after": [c.get("value") for c in new_claims],
                "source": "code",
                "version": 1,
            }
        )
        return {"claims": new_claims, "trace": trace}

    trace.append(
        {
            "rule_id": "RECENT-02",
            "stage": "project",
            "attribute": attribute,
            "action": "rewrite",
            "severity": "repair",
            "reason": "进程字段：本轮推断弱于上一版结论且未标注推翻，沿用上一版",
            "before": base_claim.get("value"),
            "after": [c.get("value") for c in new_claims],
            "source": "code",
            "version": 1,
        }
    )
    return {"claims": [base_claim], "trace": trace}


LEVEL_ORDER: tuple[str, ...] = ("H", "A", "B", "C", "D", "待识别")

ANCHOR_MIN_CHARS = 6
"""等级变化锚点匹配的最短连续片段。短于此的片段在任何长文本里都会偶然命中。"""


def _norm_text(text: Any) -> str:
    return "".join(str(text or "").split())


def anchors_new_records(evidence: Sequence[str], new_record_text: str) -> bool:
    """定级结论里的证据能否在**本轮新增记录**正文里找到出处。

    判据是「至少一条证据有一段 ≥ :data:`ANCHOR_MIN_CHARS` 的连续文字出现在新记录里」。
    它不需要逐字全等（证据是「出处 + 信号 + 指向」的整合陈述），只要有一段能在新记录里
    落地，就说明这次改判确实建立在新的材料上。
    """
    haystack = _norm_text(new_record_text)
    if not haystack:
        return False
    for item in evidence:
        text = _norm_text(item)
        if len(text) < ANCHOR_MIN_CHARS:
            continue
        if text in haystack:
            return True
        for start in range(0, len(text) - ANCHOR_MIN_CHARS + 1):
            if text[start : start + ANCHOR_MIN_CHARS] in haystack:
                return True
    return False


def guard_level(
    *,
    level: str,
    previous_level: str,
    stage: str,
    previous_stage: str,
    evidence: Sequence[str],
    new_record_text: str,
    disabled: Iterable[str] = (),
) -> dict[str, Any]:
    """RECENT-03：等级变化必须锚定本轮新增记录；阶段只能前进。

    返回 ``{"level", "stage", "reverted", "reason"}``。``reverted`` 非空表示发生了回退，
    调用方据此留痕。
    """
    off = {str(item).strip() for item in disabled}
    if "RECENT-03" in off:
        return {"level": level, "stage": stage, "reverted": "", "reason": ""}

    why = ""
    final_level = level
    if (
        previous_level
        and level
        and level != previous_level
        and not anchors_new_records(evidence, new_record_text)
    ):
        final_level = previous_level
        why = f"等级由 {previous_level} 变为 {level}，但在本轮新增记录中找不到支撑，回退 {previous_level}"

    final_stage = stage
    order = ("lead", "opportunity")
    if previous_stage in order and stage in order:
        if order.index(stage) < order.index(previous_stage):
            final_stage = previous_stage
            why = (why + "；" if why else "") + f"阶段由 {previous_stage} 回退到 {stage}，阶段只能前进"

    return {"level": final_level, "stage": final_stage, "reverted": why, "reason": why}

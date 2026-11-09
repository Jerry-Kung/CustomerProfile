"""付款方式簇：贷款口径与「未提及即全款」的默认。

三条规则的分工是这个簇设计的全部要点（业务口径：一线反馈约九成全款、一成贷款，
模型把「价格敏感」一类线索误读成了贷款，贷款占比因此明显偏高）：

- ``PAY-01`` 是**信源**问题——补贴、优惠、砍价、收入档这类信号不描述付款方式，
  只由它们撑起来的贷款断言整条拒收；
- ``PAY-02`` 是**默认**问题——一手与登记记录里都没提贷款/分期，就按全款落值，
  但**标成低置信**，让销售点开看到「未提及，按全款处理」而不是一条确定事实；
- ``PAY-03`` 是**表达**问题——只由弱信号（月供、利率、金融政策、首付）推出的贷款
  保留，但置信度压低、说明里注明「推断可能存在贷款倾向，但尚未确认」。

判据只看**证据文本**（``basis_of_inference`` / ``reasoning_chain`` / 原话），
不看取值本身：``loan`` 这个值对三条规则是同一个字面量，区别全在它凭什么被写出来。

**提示词正文不在本簇**：``payment_method`` 的取证方向由 ``SPEC-20`` 承载
（登记表自检要求同一字段只能有一条 prompt 规则，两条各写一段会静默丢一段）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..actions import Action
from ..registry import Rule

FIELD = "payment_method"
"""本簇唯一作用的字段。"""

ORDER_CHANNELS: tuple[str, ...] = ("订单记录", "订单金融方案")
"""登记档里会直接写明付款方式的渠道。它们没写，就是「不知道」，不能用先验盖上去。"""

RULES: tuple[Rule, ...] = (
    Rule(
        id="PAY-01",
        version=1,
        title="价格与预算线索不是贷款依据",
        content_kind="spec",
        channels=("code",),
        scope=(FIELD,),
        stages=("project",),
        severity="repair",
        default_action="reject",
        message_internal=(
            "贷款断言的证据只落在补贴/优惠/砍价/比价/收入档/车型价格标签上：整条拒收，"
            "交 PAY-02 按全款默认；只要有一条真正的金融线索（贷款/分期/免息/月供/利率/"
            "首付/订单金融方案）就不拒"
        ),
        message_customer="",
        owner="business",
        status="active",
        cluster="finance",
        tests=(
            "tests/rules/test_finance.py::test_price_only_loan_claim_is_rejected",
            "tests/rules/test_finance.py::test_subsidy_claim_does_not_reject_loan_with_finance_hint",
        ),
        legacy_codes=("e_price_only_loan",),
    ),
    Rule(
        id="PAY-02",
        version=1,
        title="未提及贷款即按全款",
        content_kind="spec",
        channels=("code",),
        scope=(FIELD,),
        stages=("project",),
        severity="warn",
        default_action="rewrite",
        message_internal=(
            "一手记录与登记记录都没有贷款/分期相关表述，且订单记录也没写明分期："
            "落「全款」，置信档记 inferred_low，说明写「沟通中未提及贷款或分期安排」"
        ),
        message_customer="",
        owner="business",
        status="active",
        cluster="finance",
        tests=(
            "tests/rules/test_finance.py::test_missing_payment_method_defaults_to_full_payment",
            "tests/rules/test_finance.py::test_order_finance_record_blocks_full_payment_default",
        ),
        legacy_codes=("f_payment_default_full",),
    ),
    Rule(
        id="PAY-03",
        version=1,
        title="弱金融信号只给倾向",
        content_kind="spec",
        channels=("code",),
        scope=(FIELD,),
        stages=("gate",),
        severity="warn",
        default_action="downgrade",
        message_internal=(
            "贷款结论只由月供/利率/免息/贷款期限/首付比例/金融政策这类间接表述支撑："
            "置信度压到 0.6 以下，说明注明「推断可能存在贷款倾向，但尚未确认」"
        ),
        message_customer="",
        owner="business",
        status="active",
        cluster="finance",
        tests=(
            "tests/rules/test_finance.py::test_weak_signal_loan_is_downgraded_and_annotated",
            "tests/rules/test_finance.py::test_explicit_loan_is_left_alone",
        ),
        legacy_codes=("f_payment_weak_signal",),
    ),
)

# ---------------------------------------------------------------- 词表
#
# 三类词表都只收「客户或记录里真的会出现的说法」。宁可漏判（保持原状）也不硬判：
# 误拒一条真正的贷款结论，比放过一条模糊结论代价大得多。

STRONG_LOAN_PHRASES: tuple[str, ...] = (
    "贷款",
    "按揭",
    "分期付款",
    "分期购车",
    "走金融",
    "金融方案",
    "金融政策",
    "厂家金融",
    "银行金融",
    "免息",
    "月供",
    "月还款",
    "利率",
    "利息",
    "首付",
    "贷几年",
    "贷多少",
    "征信",
    "审批",
    "贴息",
    "融资租赁",
)
"""**真金融线索**：出现任意一条，贷款结论就不算「凭空推断」。

命中它只说明「证据里有金融成分」，不说明强或弱——强弱由
:data:`WEAK_LOAN_PHRASES` 单独判（PAY-03 用它）。
"""

WEAK_LOAN_PHRASES: tuple[str, ...] = (
    "月供",
    "月还款",
    "利率",
    "利息",
    "免息",
    "首付",
    "贴息",
    "金融政策",
    "金融方案",
    "厂家金融",
    "银行金融",
    "分期政策",
)
"""**间接线索**：客户可能在问别人的方案、也可能只是在比价，单独支撑不起确定结论。

``贷款`` / ``按揭`` / ``分期付款`` 刻意**不在**此列——客户说出这几个词本身就是明确表态。
"""

EXPLICIT_LOAN_PHRASES: tuple[str, ...] = (
    "贷款",
    "按揭",
    "分期付款",
    "分期购车",
    "走金融",
)
"""明确表态：客户自己说了贷款/按揭/分期。命中即不适用 PAY-03。"""

PRICE_ONLY_PHRASES: tuple[str, ...] = (
    "补贴",
    "国补",
    "地补",
    "以旧换新",
    "置换补贴",
    "优惠",
    "活动价",
    "折扣",
    "赠品",
    "送礼",
    "便宜",
    "贵",
    "砍价",
    "还价",
    "最低价",
    "底价",
    "降价",
    "报价",
    "落地价",
    "价格敏感",
    "消费水平",
    "消费等级",
    "消费档",
    "收入",
    "低收入",
    "预算",
    "车型价格",
    "返利",
    "团购",
    "省钱",
)
"""**与付款方式无关的价格/预算线索**：只由它们推出的贷款结论按 PAY-01 拒收。

这份表就是业务方点名的误判来源（补贴、优惠、低收入、砍价/嫌贵）。新增时要问一句：
「这条能不能独立说明客户打算怎么付款？」不能的才收。
"""

ANNOTATION = "推断可能存在贷款倾向，但尚未确认"
"""PAY-03 追加进说明的固定措辞（业务方给定原文）。"""

WEAK_CONFIDENCE_CAP = 0.55
"""PAY-03 之后的最大置信度。低于 0.6 是业务口径「置信度不宜过高」的落点。"""

DEFAULT_CONFIDENCE = 0.65
"""PAY-02 落「全款」时的置信度（用户口径 2026-11-03）。

不写 1.0、也不写 0.25：它是**先验**而非猜测——「九成全款」有业务数据支撑；
但也不是客户说过的话，给满分会让销售把一条默认值当成客户已表态。
"""

DEFAULT_SUMMARY = "沟通记录与登记信息中未提及贷款或分期安排，按全款购车处理"
DEFAULT_STEPS: tuple[str, ...] = (
    "沟通记录、登记信息与订单记录中都没有贷款或分期表述",
    "按客户的付款方式先记为全款",
    "如客户后续提及贷款或分期，以客户确认的为准",
)


# ---------------------------------------------------------------- 判据


def _texts(claim: Any) -> list[str]:
    """一条断言里**作为依据**的文本：原文切片与推断依据。

    **刻意不收 ``value`` 与 ``reasoning_steps``**（2026-11-03 实测踩到）：二者都是
    模型自己的结论——推理链里写着「推倾向贷款」「可能倾向分期」，结论里就是
    ``loan``。把它们当依据喂给判据，等于「模型说贷款」就证明了「有金融线索」，
    PAY-01 永远拒不掉任何一条。判据只看原文说了什么。
    """
    parts: list[str] = []
    for name in ("quote", "source_quote", "anchor"):
        raw = getattr(claim, name, "")
        if isinstance(raw, str) and raw.strip():
            parts.append(raw)
    for name in ("basis_of_inference",):
        for item in getattr(claim, name, ()) or ():
            text = str(item)
            if text.strip():
                parts.append(text)
    return parts


def _has_phrase(texts: Iterable[str], phrases: Iterable[str]) -> bool:
    return any(phrase in text for text in texts for phrase in phrases)


def has_finance_hint(claim: Any) -> bool:
    """证据里有没有真金融线索（强或弱都算）。"""
    return _has_phrase(_texts(claim), STRONG_LOAN_PHRASES)


def is_price_only(claim: Any) -> bool:
    """证据全部落在价格/预算线索里，且没有一条金融线索。

    「全部落在」而不是「含有一句」：一条贷款结论常常同时写了补贴和征信，那是有金融
    依据的，不能因为提过补贴就拒掉——PAY-01 拦的是**只**凭价格信号下的结论。
    """
    if has_finance_hint(claim):
        return False
    return _has_phrase(_texts(claim), PRICE_ONLY_PHRASES)


def is_weak_only(claim: Any) -> bool:
    """证据只由间接线索支撑：没有明确的贷款/按揭/分期表态，只有月供/利率一类说法。"""
    texts = _texts(claim)
    if not _has_phrase(texts, WEAK_LOAN_PHRASES):
        return False
    return not _has_phrase(texts, EXPLICIT_LOAN_PHRASES)


# ---------------------------------------------------------------- 规则函数


def check_loan_evidence(attribute: str, group: Sequence[Any]) -> tuple[list[Any], list[Action]]:
    """project 段（PAY-01）：只凭价格/预算线索的贷款断言整条拒收。

    返回 ``(留下的断言, 动作)``，与 :func:`customer_profile.rules.clusters.income.check_income_tag`
    同形——投影层拿到留下的断言后照常走后续裁决；动作只记留痕，不改 ``facts``
    （此时取值还没落定，见 ``_apply_field`` 对 reject 的处理）。
    """
    # rule: PAY-01 价格与预算线索不是贷款依据
    if attribute != FIELD:
        return list(group), []
    kept: list[Any] = []
    actions: list[Action] = []
    for claim in group:
        if str(getattr(claim, "value", "")).strip() != "loan" or not is_price_only(claim):
            kept.append(claim)
            continue
        actions.append(
            Action(
                rule="PAY-01",
                attribute=attribute,
                action="reject",
                severity="repair",
                reason="贷款结论只由补贴/优惠/价格/收入档支撑，这些信号不描述付款方式",
                remove=False,
                payload={
                    "attribute": attribute,
                    "value": getattr(claim, "value", ""),
                    "rule": "e_price_only_loan",
                    "reason": "贷款结论只由补贴/优惠/价格/收入档支撑，这些信号不描述付款方式",
                },
            )
        )
    return kept, actions


def default_full_payment_action(attribute: str = FIELD) -> Action:
    """PAY-02 的落值动作：写入全款。

    置信档不在动作里定（``affects_basis=False``）：执行器只会把 ``fact`` 抬成
    ``inferred_high``，而这里要的是 ``inferred_low``。由投影层在应用后单独落档，
    与姓名兜底同一做法。
    """
    # rule: PAY-02 未提及贷款即按全款
    return Action(
        rule="PAY-02",
        attribute=attribute,
        action="rewrite",
        severity="warn",
        reason="沟通记录与登记信息中未提及贷款或分期，按全款处理（低置信，待确认）",
        value="full_payment",
        affects_basis=False,
    )


def should_default(facts: Mapping[str, Any], claims: Sequence[Any]) -> bool:
    """PAY-02 的适用判据：字段为空、且没有任何登记档写过付款方式。

    订单记录是**登记档事实**：它没写付款方式，说明「不知道」，不能拿先验盖上去。
    """
    if facts.get(FIELD) not in (None, ""):
        return False
    # 看的是「这批记录里有没有订单渠道」，不是「付款方式字段有没有订单渠道的断言」：
    # 订单记录**存在却没提到**付款方式，恰恰说明这条信息拿不到，不能用先验补。
    for claim in claims:
        if getattr(claim, "channel", "") in ORDER_CHANNELS:
            return False
    return True


def default_detail() -> dict[str, Any]:
    """PAY-02 条目的 ``reasoning_detail``：说清「这是默认，不是客户说的」。"""
    return {
        "summary": DEFAULT_SUMMARY,
        "steps": list(DEFAULT_STEPS),
        "evidence": [],
        "confidence": DEFAULT_CONFIDENCE,
    }


def check_weak_signal(entries: Sequence[Any]) -> list[Action]:
    """gate 段（PAY-03）：只由弱信号支撑的贷款条目加注「未确认」并压低置信。

    判据作用在**已写好的 Notes 条目**上（说明/步骤/证据拼起来），因为投影层的断言
    与 gate 的条目是两个对象；两处共用同一份词表与 :func:`is_weak_only` 的口径。
    """
    # rule: PAY-03 弱金融信号只给倾向
    actions: list[Action] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("attribute_key")) != FIELD:
            continue
        if str(entry.get("attribute_value")) != "loan":
            continue
        detail = entry.get("reasoning_detail")
        detail = dict(detail) if isinstance(detail, Mapping) else {}
        # 与 :func:`_texts` 同口径：只看**证据位**。说明与步骤是结论，写成什么样都含
        # 「贷款」二字，拿它们判强弱会把每一条都判成明确表态。
        texts = [str(item) for item in (detail.get("evidence") or []) if str(item).strip()]
        if not texts:
            texts = [str(detail.get("summary") or "")].copy()
        if not _has_phrase(texts, WEAK_LOAN_PHRASES):
            continue
        if _has_phrase(texts, EXPLICIT_LOAN_PHRASES):
            continue

        summary = str(detail.get("summary") or "").strip()
        if ANNOTATION not in summary:
            summary = f"{summary}；{ANNOTATION}" if summary else ANNOTATION
        steps = [str(item) for item in (detail.get("steps") or []) if str(item).strip()]
        if ANNOTATION not in "".join(steps):
            steps.append(ANNOTATION)
        try:
            current = float(detail.get("confidence"))
        except (TypeError, ValueError):
            current = WEAK_CONFIDENCE_CAP
        actions.append(
            Action(
                rule="PAY-03",
                attribute=FIELD,
                action="rewrite",
                severity="warn",
                reason="贷款结论只由月供/利率/首付等间接表述支撑，降为倾向并加注未确认",
                stage="gate",
                target="note",
                payload={
                    "reasoning_detail": {
                        "summary": summary,
                        "steps": steps,
                        "evidence": [
                            str(i) for i in (detail.get("evidence") or []) if str(i).strip()
                        ],
                        "confidence": min(WEAK_CONFIDENCE_CAP, current),
                    }
                },
            )
        )
    return actions

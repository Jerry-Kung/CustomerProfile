"""PAY 簇：付款方式（贷款 / 全款）。

业务背景（2026-11-03 用户口径）：一线反馈约九成全款、一成贷款，而模型把补贴、砍价、
收入档这类**价格敏感**信号误读成了贷款，贷款占比明显偏高。三条规则各管一段：

- ``PAY-01`` 拒掉只凭价格线索的贷款断言（信源）；
- ``PAY-02`` 在未提及时按全款落值（默认，低置信）；
- ``PAY-03`` 把只凭月供/利率一类弱信号推出的贷款降为倾向并加注（表达）。

反例组同样重要：误拒一条真的ment 的贷款结论，比放过一条模糊结论代价大得多——
客户确实要贷款却被写成全款，销售会按全款口径谈价，成交时才发现要换金融方案。
"""

from __future__ import annotations

import json

from customer_profile import projection as proj
from customer_profile.rules import NotesState, apply
from customer_profile.rules.clusters import finance as fin
from customer_profile.workflows import customer_profile_pipeline as pipe


def claim(value, *, channel="试驾录音信息", basis="fact", quote="", hints=(), steps=(), conf=0.9):
    return proj.Claim(
        attribute="payment_method",
        value=value,
        channel=channel,
        basis=basis,
        quote=quote,
        basis_of_inference=tuple(hints),
        reasoning_steps=tuple(steps),
        confidence=conf,
    )


def note(value, *, summary="", steps=None, evidence=None, confidence=0.9):
    return {
        "attribute_key": "payment_method",
        "attribute_value": value,
        "reasoning_detail": {
            "summary": summary,
            "steps": steps or [],
            "evidence": evidence or [],
            "confidence": confidence,
        },
    }


# ---------------------------------------------------------------- PAY-01 正反例


def test_price_only_loan_claim_is_rejected():
    """正例：贷款结论只凭置换补贴与「价格敏感」推出来 → 拒收，随后按全款默认。

    实测来源：``uat1-15818891947-20261030``，原产物「客户询问置换补贴…反复确认最低配」
    → ``loan``，置信 0.3。
    """
    result = proj.project(
        [
            claim(
                "loan",
                basis="inferred_low",
                hints=("客户询问置换补贴「可能会有1万5」", "客户对价格敏感、反复确认最低配"),
                steps=("对补贴与价格高度敏感", "推倾向贷款降低首付压力"),
                conf=0.35,
            )
        ]
    )
    assert result.facts.get("payment_method") == "full_payment"
    assert result.basis.get("payment_method") == proj.INFERRED_LOW
    assert any(item.get("rule") == "e_price_only_loan" for item in result.rejected)
    assert "payment_method" in result.defaulted


def test_subsidy_claim_does_not_reject_loan_with_finance_hint():
    """反例：同一句话里既提补贴、又提征信或免息，是有金融依据的，不得拒。

    这是本规则最容易写错的形态——用「含不含补贴」当场判据，会把一条真的贷款结论误杀。
    """
    result = proj.project(
        [
            claim(
                "loan",
                quote="补贴两万，我这征信能贷多少",
                hints=("客户询问置换补贴金额", "客户确认自己的征信可以贷款"),
                conf=0.85,
            )
        ]
    )
    assert result.facts.get("payment_method") == "loan"
    assert not any(item.get("rule") == "e_price_only_loan" for item in result.rejected)


# ---------------------------------------------------------------- PAY-02 正反例


def test_missing_payment_method_defaults_to_full_payment():
    """正例：全案没提付款方式 → 落全款、低置信、说明写明「这是默认」。"""
    result = proj.project(
        [proj.Claim(attribute="occupation", value="教师", channel="试驾录音信息",
                    basis="fact", quote="我是老师")]
    )
    assert result.facts.get("payment_method") == "full_payment"
    assert result.basis.get("payment_method") == proj.INFERRED_LOW
    detail = next(
        n["reasoning_detail"] for n in result.notes_updates
        if n["attribute_key"] == "payment_method"
    )
    assert detail["summary"] == fin.DEFAULT_SUMMARY
    assert detail["confidence"] == fin.DEFAULT_CONFIDENCE
    # 不得借用「有原文依据」这类推断措辞——它是默认值，不是从原文推出来的。
    assert "原文" not in detail["summary"]


def test_order_finance_record_blocks_full_payment_default():
    """反例：订单记录存在但没写付款方式时，不得拿先验盖上去。

    订单是登记档事实，它没写就是「不知道」；此时默认全款等于把一条系统记录改写成
    了一条业务假设。
    """
    result = proj.project(
        [
            proj.Claim(attribute="occupation", value="教师", channel="试驾录音信息",
                       basis="fact", quote="我是老师"),
            proj.Claim(attribute="viewing_progress", value="deep_think",
                       channel="订单记录", basis="fact"),
        ]
    )
    assert "payment_method" not in result.facts


def test_empty_tier_gets_no_payment_default():
    """反例：``empty`` 分诊档（数据全空）不落默认全款——那是凭空编结论。"""
    result = pipe.project_claims(
        json.dumps({"claims": []}, ensure_ascii=False), tier="empty"
    )
    facts = json.loads(result["facts_json"])
    assert "payment_method" not in facts
    assert not any(
        n["attribute_key"] == "payment_method"
        for n in json.loads(result["notes_updates_json"])
    )


def test_payment_method_is_not_offered_to_l2_fill_in():
    """默认值字段不交兜底补全：候选里不得再出现 ``payment_method`` 的缺字段条目。"""
    result = proj.project([proj.Claim(attribute="occupation", value="教师")])
    ids = {item["attribute_key"] for item in proj.build_l2_candidates([], result)}
    assert "payment_method" not in ids


# ---------------------------------------------------------------- PAY-03 正反例


def test_weak_signal_loan_is_downgraded_and_annotated():
    """正例：只凭「月供」「利率」推出的贷款 → 置信压到 0.6 以下并加注未确认。"""
    state = NotesState(
        notes=[
            note(
                "loan",
                summary="客户询问月供与贷款利率",
                evidence=["客户问「月供大概多少」「利率怎么算」"],
                confidence=0.9,
            )
        ]
    )
    actions = fin.check_weak_signal(state.notes)
    assert [a.rule for a in actions] == ["PAY-03"]
    apply(state, actions)
    detail = state.notes[0]["reasoning_detail"]
    assert detail["confidence"] <= 0.6
    assert fin.ANNOTATION in detail["summary"]
    assert fin.ANNOTATION in "".join(detail["steps"])


def test_explicit_loan_is_left_alone():
    """反例：客户原话「这个贷款也是怎么样」是明确表态，不降级、不加注。

    实测来源：``uat6-ctrl-20261030`` 的 13760736409，原产物置信 0.85。
    """
    state = NotesState(
        notes=[
            note("loan", summary="有原文依据：这个贷款也是怎么样",
                 evidence=["这个贷款也是怎么样"], confidence=0.85)
        ]
    )
    assert fin.check_weak_signal(state.notes) == []
    assert fin.ANNOTATION not in state.notes[0]["reasoning_detail"]["summary"]


def test_strong_evidence_loan_is_untouched_in_projection():
    """反例（投影层）：带原文锚点的贷款断言不受三条规则影响。"""
    result = proj.project([claim("loan", quote="这个贷款也是怎么样")])
    assert result.facts.get("payment_method") == "loan"
    assert result.basis.get("payment_method") == proj.FACT
    assert "payment_method" not in result.defaulted


# ---------------------------------------------------------------- 提示词（SPEC-20）


def test_payment_note_rendered_inline():
    """SPEC-20 的正文必须贴着 ``payment_method`` 出现在《目标字段清单》里。"""
    guide = pipe.field_inference_guide()
    lines = guide.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("- `payment_method`"))
    assert lines[index + 1].startswith("  - 取证范围：")
    assert "尚未确认" in lines[index + 1]


def test_price_signals_named_in_prompt_as_excluded():
    """提示词里必须点名禁止的信源，否则模型读不到这条业务口径。"""
    text = pipe.field_inference_guide()
    for phrase in ("补贴", "砍价", "低收入", "消费档"):
        assert phrase in text, f"提示词未点名排除信源：{phrase}"

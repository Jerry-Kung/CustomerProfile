"""时近性规则簇（``RECENT-01`` / ``RECENT-02`` / ``RECENT-03``）的回归。

三条规则都是**静默生效**的：它们让某条断言「不进入账本」或「退回上一版」，出错不会报错，
只会让一个字段悄悄沿用旧值、或一个等级悄悄回退。因此每条都要有正例与反例成对钉住。
"""

from __future__ import annotations

import json

from customer_profile.incremental.merge import merge_claims
from customer_profile.rules.clusters import recency as rc
from customer_profile.workflows import customer_profile_pipeline as pipe

BASE = "上一版画像"


def _claim(attribute, value, basis="inferred_high", channel=BASE, **kwargs):
    entry = {
        "attribute": attribute,
        "value": value,
        "basis": basis,
        "record_channel": channel,
        "observed_at": "2026-11-01T10:00:00",
    }
    entry.update(kwargs)
    return entry


def _row(channel="OntologyLeadInfo", source_key=None, version=None):
    return {
        "channel": channel,
        "source_key": source_key,
        "source_version": version,
        "created_at": "2026-11-02T10:00:00",
        "raw_payload": {"data": channel},
    }


# ---------------------------------------------------------------- RECENT-01

def test_steady_field_keeps_previous_conclusion():
    """稳态字段（年龄）上只有一条弱信号：沿用上一版，新断言不进入账本。"""
    decision = rc.decide_field(
        attribute="age",
        base_claim=_claim("age", "35-40", basis="fact"),
        new_claims=[_claim("age", "30岁左右", basis="inferred_low", channel="朋友圈截图内容信息")],
    )
    values = [c["value"] for c in decision["claims"]]
    assert "35-40" in values
    assert "30岁左右" not in values
    assert decision["trace"] and decision["trace"][0]["rule_id"] == "RECENT-01"


def test_steady_field_takes_new_fact():
    """反例：稳态字段上来了更硬的新事实，必须以新事实为准。"""
    decision = rc.decide_field(
        attribute="age",
        base_claim=_claim("age", "35-40", basis="inferred_high"),
        new_claims=[_claim("age", "42", basis="fact", channel="销售人员人工确认的客户Notes信息")],
    )
    values = [c["value"] for c in decision["claims"]]
    assert "42" in values
    assert not decision["trace"]


# ---------------------------------------------------------------- RECENT-02

def test_process_field_later_one_wins():
    """进程字段（预算）：同档来源后到者胜，上一版结论退出。"""
    decision = rc.decide_field(
        attribute="budget_range",
        base_claim=_claim("budget_range", "18-20万", basis="inferred_high"),
        new_claims=[_claim("budget_range", "20-25万", basis="inferred_high", channel="销售跟进记录")],
    )
    values = [c["value"] for c in decision["claims"]]
    assert "20-25万" in values
    assert "18-20万" not in values


def test_process_field_weaker_signal_keeps_base():
    """反例：进程字段上本轮只有更弱的推断、且未标注推翻，沿用上一版。"""
    decision = rc.decide_field(
        attribute="budget_range",
        base_claim=_claim("budget_range", "18-20万", basis="fact"),
        new_claims=[_claim("budget_range", "30万左右", basis="inferred_low", channel="朋友圈截图内容信息")],
    )
    values = [c["value"] for c in decision["claims"]]
    assert values == ["18-20万"]


def test_process_field_weak_but_overturning_is_confirmed_not_dropped():
    """带「推翻」标注的弱信号：降置信保留并转人工确认，而不是静默丢弃。"""
    decision = rc.decide_field(
        attribute="intended_model",
        base_claim=_claim("intended_model", "某款 A", basis="fact"),
        new_claims=[
            _claim("intended_model", "某款 B", basis="inferred_low", channel="销售跟进记录", supersedes=True)
        ],
    )
    values = [c["value"] for c in decision["claims"]]
    assert values == ["某款 B"]
    assert decision["claims"][0]["overturns_base"] is True
    assert decision["trace"][0]["action"] == "require_confirm"


def test_disabled_rule_leaves_base_alone():
    """``RULES_DISABLED=RECENT-02`` 时规则不生效（单条回滚）。"""
    decision = rc.decide_field(
        attribute="budget_range",
        base_claim=_claim("budget_range", "18-20万", basis="fact"),
        new_claims=[_claim("budget_range", "30万", basis="inferred_low")],
        disabled=("RECENT-02",),
    )
    values = [c["value"] for c in decision["claims"]]
    assert "30万" in values


# ---------------------------------------------------------------- RECENT-03

def test_level_change_without_new_evidence_reverts():
    """等级改了、但证据在新记录里找不到出处：回退上一版并给出理由。"""
    guard = rc.guard_level(
        level="A",
        previous_level="B",
        stage="lead",
        previous_stage="lead",
        evidence=["客户明确表示本月底前要提车"],
        new_record_text="客户问了一下保养政策，暂时没有购买计划",
    )
    assert guard["level"] == "B"
    assert "回退" in guard["reverted"]


def test_level_change_with_new_evidence_survives():
    """反例：新证据就在本轮新增记录里，等级变化保留。"""
    guard = rc.guard_level(
        level="A",
        previous_level="B",
        stage="lead",
        previous_stage="lead",
        evidence=["客户明确表示本月底前要提车"],
        new_record_text="客户明确表示本月底前要提车，让销售准备合同",
    )
    assert guard["level"] == "A"
    assert guard["reverted"] == ""


def test_stage_never_goes_backwards():
    guard = rc.guard_level(
        level="B",
        previous_level="B",
        stage="lead",
        previous_stage="opportunity",
        evidence=[],
        new_record_text="",
    )
    assert guard["stage"] == "opportunity"
    assert "阶段只能前进" in guard["reverted"]


def test_guard_is_noop_without_previous_level():
    """首次运行（无上一版）不做任何守卫。"""
    guard = rc.guard_level(
        level="A",
        previous_level="",
        stage="lead",
        previous_stage="",
        evidence=[],
        new_record_text="",
    )
    assert guard["level"] == "A" and guard["reverted"] == ""


def test_guard_disabled_leaves_level_alone():
    guard = rc.guard_level(
        level="A",
        previous_level="B",
        stage="lead",
        previous_stage="lead",
        evidence=["客户明确表示本月底前要提车"],
        new_record_text="",
        disabled=("RECENT-03",),
    )
    assert guard["level"] == "A"


# ---------------------------------------------------------------- 端到端（管线收口）

def _records_json(*rows):
    return json.dumps(list(rows), ensure_ascii=False)


def _record(channel, content, manifest="OntologyLeadFollowRecord"):
    """记录层的一条记录：``channel`` 是清单渠道键（身份键按它算），``source_name`` 是中文名。"""
    return {
        "record_id": f"rec_{channel}",
        "channel": manifest,
        "source_name": channel,
        "source_class": "declared",
        "status": "ok",
        "created_at": "2026-11-02T10:00:00",
        "source_key": "k1",
        "source_version": 1,
        "content": content,
        "content_kind": "json",
    }


def _ctx(previous_level="B", new_key="OntologyLeadFollowRecord|k1|v1"):
    from customer_profile.incremental import context as ictx

    return ictx.dumps(
        ictx.build(
            new_keys=[new_key],
            new_logical=[new_key.rsplit("|", 1)[0]],
            base_fields=[],
            base_facts={},
            base_sections={},
            previous_level=previous_level,
        )
    )


def _assess(level="A"):
    return json.dumps(
        {
            "level": level,
            "confidence": 0.8,
            "time_window": "14天内",
            "dimensions": {
                "want_to_buy": {"value": "换车", "evidence": ["客户说想换一台大空间的车接送孩子"]},
                "can_to_buy": {"value": "预算匹配", "evidence": []},
                "when_to_buy": {"value": "九月前", "evidence": []},
            },
            "homework_evidence": ["客户已到店试驾并给出正向反馈"],
            "steps": ["客户说想换一台大空间的车接送孩子"],
            "summary": "需求明确",
        },
        ensure_ascii=False,
    )


def test_pipeline_reverts_level_when_new_records_do_not_support_it():
    """接进管线后：新记录里没有支撑，定级收口把等级回退上一版。"""
    records = _records_json(_record("销售跟进记录", "客户问了一下保养政策"))
    outcome = pipe.apply_level(
        records,
        "{}",
        _assess("A"),
        stage="lead",
        incremental_ctx=_ctx(previous_level="C"),
    )
    assert outcome["level"] == "C"
    assert outcome["level_reverted"]


def test_pipeline_keeps_level_when_new_records_support_it():
    records = _records_json(
        _record("销售跟进记录", "客户说想换一台大空间的车接送孩子，九月前要买")
    )
    outcome = pipe.apply_level(
        records,
        "{}",
        _assess("A"),
        stage="lead",
        incremental_ctx=_ctx(previous_level="C"),
    )
    assert outcome["level"] == "A"
    assert outcome["level_reverted"] == ""


def test_pipeline_without_incremental_ctx_has_no_guard():
    """非增量运行：守卫不生效，输出与接入前一致。"""
    records = _records_json(_record("销售跟进记录", "客户问了一下保养政策"))
    outcome = pipe.apply_level(records, "{}", _assess("A"), stage="lead")
    assert outcome["level"] == "A"
    assert outcome["level_reverted"] == ""


# ---------------------------------------------------------------- 合并账本

def test_merge_retires_claims_of_removed_channel():
    """底座断言依赖的渠道整条从本轮清单消失：该断言退场。"""
    outcome = merge_claims(
        base_fields={"fields": [_claim("occupation", "教师", channel="销售跟进记录")]},
        new_fields={"fields": []},
        current_rows=[_row("OntologyLeadInfo")],
    )
    assert outcome["retired"] == 1
    assert json.loads(outcome["claims_json"])["fields"] == []


def test_merge_keeps_base_claim_when_channel_still_present():
    outcome = merge_claims(
        base_fields={"fields": [_claim("occupation", "教师", channel="销售跟进记录")]},
        new_fields={"fields": []},
        current_rows=[_row("OntologyLeadFollowRecord", "k1", 1)],
    )
    assert outcome["retired"] == 0
    fields = json.loads(outcome["claims_json"])["fields"]
    assert [f["value"] for f in fields] == ["教师"]

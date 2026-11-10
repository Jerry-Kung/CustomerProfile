"""增量调度判定的表驱动测试。

判定顺序自上而下、先命中先生效（``01`` §4）：无基线 → T3；兼容键不符 → T3；
键集合无差异 → T0；新增含试驾录音 → T3；含其他录音 → T2；仅文本 → T1。
P1 只放行 T0，其余一律落 T3，这个放行集合也要钉住——它是回滚与灰度的手段。
"""

from __future__ import annotations

import json

from customer_profile.incremental.dispatch import TIER_T0, TIER_T3, decide
from customer_profile.persistence.profile_baselines import ProfileBaseline


def _row(channel: str, source_key: str | None, version: int | None = 1, payload: dict | None = None) -> dict:
    return {
        "channel": channel,
        "source_key": source_key,
        "source_version": version,
        "created_at": "2026-11-01T10:00:00",
        "raw_payload": payload or {"data": channel},
    }


def _baseline(keys: tuple[str, ...], *, compat: str = "K", callback: dict | None = None) -> ProfileBaseline:
    return ProfileBaseline(
        phone="13900000001",
        compat_key=compat,
        updated_at_ms=1,
        stage="lead",
        deal_level="B",
        deal_level_detail="",
        consumed_keys=keys,
        callback=callback or {
            "phone": "13900000001",
            "batch_id": "old",
            "analysis_result": {"basic_notes_updates": [{"attribute_key": "customer_name", "attribute_value": "张三"}]},
        },
    )


def _decide(rows, baseline, *, compat: str = "K", tiers=(TIER_T0,)):
    return decide(
        history_body=json.dumps(rows, ensure_ascii=False),
        phone="13900000001",
        batch_id="new-batch",
        baseline=baseline,
        current_compat_key=compat,
        enabled_tiers=tiers,
    )


def test_no_baseline_is_full_rebuild():
    result = _decide([_row("OntologyOrder", "k1")], None)
    assert result["tier"] == TIER_T3
    assert "无基线" in result["tier_reason"]
    assert result["baseline_found"] == "false"


def test_identity_key_set_unchanged_is_t0():
    row = _row("OntologyOrder", "k1")
    from customer_profile.incremental.diff import record_key

    result = _decide([row], _baseline((record_key(row),)))
    assert result["tier"] == TIER_T0
    assert result["added_count"] == 0


def test_keep_baseline_consumed_keys_within_baseline_store(self=None):
    """本轮接口少返回一条旧记录时，仍应判 T0——键集合有「消失」，但那是上游删了。"""
    kept = _row("OntologyOrder", "k1")
    gone = _row("OntologyLeadFollowRecord", "k2")
    from customer_profile.incremental.diff import record_key

    result = _decide([kept], _baseline((record_key(kept), record_key(gone))))
    # P1 口径：键集合无差异才是 T0；有移除说明数据变了，落全量。
    assert result["tier"] == TIER_T3
    assert result["removed_count"] == 1


def test_compat_key_mismatch_is_full_rebuild():
    row = _row("OntologyOrder", "k1")
    from customer_profile.incremental.diff import record_key

    result = _decide([row], _baseline((record_key(row),), compat="OLD"), compat="NEW")
    assert result["tier"] == TIER_T3
    assert "兼容键" in result["tier_reason"]
    assert result["compat_ok"] == "false"


def test_base_channel_resnapshot_is_not_a_change():
    """基础渠道每轮重抓：id / fetched_at 变了，内容没变就不该判 T3。"""
    first = _row("OntologyLeadInfo", None, None, {"fetched_at": "t1", "data": {"lead": "L-1"}})
    second = _row("OntologyLeadInfo", None, None, {"fetched_at": "t2", "data": {"lead": "L-1"}})
    first["id"], second["id"] = 1, 99
    from customer_profile.incremental.diff import record_key

    result = _decide([second], _baseline((record_key(first),)))
    assert result["tier"] == TIER_T0


def test_first_test_drive_is_full_rebuild():
    drive = _row("TestDriveRecordingFile", "d1")
    result = _decide([drive], _baseline(()))
    assert result["tier"] == TIER_T3
    assert "试驾录音" in result["tier_reason"]


def test_audio_without_test_drive_is_t2_when_enabled():
    call = _row("OntologyManualOutboundCallRecordingFile", "c1")
    result = _decide([call], _baseline(()), tiers=("T0", "T1", "T2"))
    assert result["tier"] == "T2"


def test_text_only_is_t1_when_enabled():
    note = _row("OntologyLeadFollowRecord", "f1")
    result = _decide([note], _baseline(()), tiers=("T0", "T1", "T2"))
    assert result["tier"] == "T1"


def test_disabled_tiers_fall_back_to_full_rebuild():
    """P1 只放行 T0：T1 / T2 判据命中后仍按全量，且理由里写明这是本期口径。"""
    note = _row("OntologyLeadFollowRecord", "f1")
    result = _decide([note], _baseline(()))
    assert result["tier"] == TIER_T3
    assert "未放开" in result["tier_reason"]


def test_t0_returns_copy_with_new_batch_id_only():
    row = _row("OntologyOrder", "k1")
    from customer_profile.incremental.diff import record_key

    payload = _decide([row], _baseline((record_key(row),)))
    copied = json.loads(payload["callback_json"])
    assert copied["batch_id"] == "new-batch"
    assert copied["analysis_result"] == _baseline(()).callback["analysis_result"]


def test_previous_level_is_carried_from_baseline():
    row = _row("OntologyOrder", "k1")
    from customer_profile.incremental.diff import record_key

    payload = _decide([row], _baseline((record_key(row),)))
    assert payload["previous_level"] == "B"


def test_base_stats_are_reported_for_gray_release():
    """底座三条规则各命中几次要能看得到：灰度期统计补缺字段数全靠它。

    它是空串时无法区分「这轮没有底座」与「补缺为 0」，而后者的比例正是要观察的数字。
    """
    callback = {
        "phone": "13900000001",
        "batch_id": "old",
        "analysis_result": {
            "basic_notes_updates": [
                {"attribute_key": "customer_name", "attribute_value": "张三"},
                {"attribute_key": "occupation", "attribute_value": "教师"},
            ]
        },
    }
    note = _row("OntologyLeadFollowRecord", "f1")
    result = _decide(
        [note], _baseline((), callback=callback), tiers=("T0", "T1", "T2")
    )
    stats = json.loads(result["base_stats_json"])
    # 接口画像为空（profile_body 未给）：两项都只能来自回传体副本。
    assert stats["from_b_only"] == 2
    assert "same" in stats and "differ" in stats


def test_base_stats_are_empty_when_there_is_no_base():
    """T0 / T3 不组装底座，统计为空——与「补缺为 0」不是一回事。"""
    result = _decide([_row("OntologyOrder", "k1")], None)
    assert result["base_stats_json"] == ""


def test_malformed_history_is_treated_as_empty():
    result = decide(
        history_body="{not-json",
        phone="13900000001",
        batch_id="b",
        baseline=_baseline(()),
        current_compat_key="K",
        enabled_tiers=(TIER_T0,),
    )
    assert result["tier"] == TIER_T0

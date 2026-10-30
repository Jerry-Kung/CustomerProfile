"""邀约个性化的资料来源、阶段与对客事实校验；使用合成业务资料。"""

from __future__ import annotations

import json

import pytest

from customer_profile.invitation_context import (
    prepare_context,
    validate_personalized_script,
)
from customer_profile.workflows import invitation as flow

from .test_invitation import OfflineServices, script_output, run_offline

AS_OF = "2026-10-30T14:00:00+08:00"


def prepare(profile=None, records=None, *, transcript="", has_recording=False):
    result = prepare_context(
        json.dumps(profile or {}),
        json.dumps(records or []),
        transcript,
        has_recording,
        "call-current",
        AS_OF,
    )
    return result, json.loads(result["communication_context"])


def test_manual_confirmation_and_profile_inference_remain_distinct_and_traceable():
    _, context = prepare(
        {
            "profile_version": "profile-3",
            "basic_notes": {
                "budget_range": {"value": "30万", "is_locked": 1},
                "core_concerns": {
                    "value": "空间",
                    "reasoning_detail": {
                        "confidence": 0.7,
                        "evidence": ["由使用场景推断，客户尚未确认"],
                    },
                },
                "annual_income": {"value": "财务隐私"},
                "residence_address": {"value": "居住地址隐私"},
            },
        }
    )
    budget, space = context["facts"]
    assert budget["value"] == "30万" and budget["basis"] == "人工确认"
    assert space["basis"] == "画像归纳，非客户原话"
    assert space["source"] == "basic_notes.core_concerns"
    assert space["confidence"] == 0.7
    assert space["evidence"] == ["由使用场景推断，客户尚未确认"]
    assert context["profile_version"] == "profile-3"
    assert "隐私" not in json.dumps(context, ensure_ascii=False)
    assert context["can_reference_previous_contact"] is False


def test_high_confidence_inferred_tags_do_not_become_prior_conversation():
    result, context = prepare(
        {
            "ai_insight": {
                "purchase_intent_level": {
                    "reasoning_detail": {
                        "summary": "原始记录中无任何有效沟通，客户本人未表达需求",
                        "confidence": 0.99,
                    }
                }
            },
            "purchase_decisions": [
                {
                    "category": "核心关注",
                    "items": [
                        {"label": "智能", "reasoning_detail": {"confidence": 0.99}},
                    ],
                }
            ],
            "deal_battle": {
                "win_conditions": [{"label": "若客户已到店，则说明配置差异"}]
            },
        }
    )
    assert result["has_previous_contact"] is False
    assert context["can_reference_previous_contact"] is False
    assert context["suggested_next_steps"][0]["basis"] == "画像建议，非已发生事实"


def test_negated_completed_contact_does_not_turn_into_a_completed_visit():
    result, context = prepare(
        {
            "ai_insight": {
                "purchase_intent_level": {
                    "reasoning_detail": {
                        "summary": "未见已到店、已试驾或实际报价证据",
                    }
                }
            },
        }
    )
    assert result["has_previous_contact"] is False
    assert context["can_reference_previous_contact"] is False


def test_profile_reported_test_drive_can_choose_followup_without_call_recording():
    result, context = prepare(
        {
            "basic_notes": {
                "viewing_progress": {"value": "已试驾"},
                "payment_method": {"value": "公户分期条件待确认"},
                "trade_in_requirement": {"value": "旧车置换待评估"},
            },
        }
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is True
    assert context["latest_call"]["has_recording"] is False
    assert "公户分期" in json.dumps(context, ensure_ascii=False)
    assert "置换" in json.dumps(context, ensure_ascii=False)


@pytest.mark.parametrize("progress", ["已下订", "已成交", "已交付"])
def test_advanced_purchase_stage_is_not_routed_as_a_new_prospect(progress):
    result, context = prepare(
        {"basic_notes": {"viewing_progress": {"value": progress}}}
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is True


def test_named_test_driven_model_is_a_reported_trial_without_requiring_a_specific_phrase():
    result, context = prepare(
        {"basic_notes": {"test_driven_models": {"value": "猛士817"}}}
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is True


def test_future_trial_appointment_is_context_without_inventing_completed_contact():
    result, context = prepare(
        records=[
            {
                "id": 42,
                "channel": "OntologyTestDriveSchedule",
                "raw_payload": {
                    "schedule_time": "2026-11-01T10:00:00+08:00",
                    "status": "1",
                },
            }
        ]
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is False
    assert context["recent_business_events"][0]["source_record_id"] == 42
    assert context["recent_business_events"][0]["fields"]["status"] == "1"
    assert context["available_store_slots"] == []


def test_locked_contact_fact_takes_precedence_over_stale_profile_absence():
    result, context = prepare(
        {
            "basic_notes": {"viewing_progress": {"value": "已试驾", "is_locked": 1}},
            "ai_insight": {
                "purchase_intent_level": {
                    "reasoning_detail": {
                        "summary": "无任何有效沟通",
                    }
                }
            },
        }
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is True


def test_business_record_without_status_is_followup_context_but_not_a_completed_event():
    result, context = prepare(
        records=[{"id": 43, "channel": "OntologyTestDriveSchedule"}]
    )
    assert result["has_previous_contact"] is True
    assert context["can_reference_previous_contact"] is False
    assert context["recent_business_events"][0]["fields"] == {}


def test_current_call_and_business_time_are_preserved_without_conflating_update_time():
    text = "客户：我在外地，回城时间不确定，请先微信联系。"
    _, context = prepare(
        records=[
            {
                "id": 40,
                "channel": "OntologyManualOutboundCallRecordingFile",
                "batch_id": "profile-batch",
                "updated_at": AS_OF,
                "raw_payload": {
                    "manual_outbound_call_id": "call-current",
                    "call_time": "2026-10-28T10:00:00+08:00",
                },
            }
        ],
        transcript=text,
        has_recording=True,
    )
    assert context["latest_call"]["text"] == text
    assert context["latest_call"]["occurred_at"] == "2026-10-28T10:00:00+08:00"
    assert context["as_of"] == AS_OF
    assert context["can_reference_previous_contact"] is True


def test_invalid_audio_does_not_supply_customer_words():
    result, context = prepare(transcript=flow.INVALID_RECORDING, has_recording=True)
    assert result["has_previous_contact"] is True
    assert context["latest_call"]["valid_content"] is False
    assert context["latest_call"]["text"] == ""
    assert context["can_reference_previous_contact"] is False


def test_call_time_prefers_the_target_call_over_an_unrelated_matching_profile_batch():
    _, context = prepare(
        records=[
            {
                "id": 1,
                "channel": "OntologyManualOutboundCallRecordingFile",
                "raw_payload": {
                    "manual_outbound_call_id": "call-current",
                    "call_time": "2026-10-28T10:00:00+08:00",
                },
            },
            {
                "id": 2,
                "channel": "AIOutboundCallRecordingFile",
                "batch_id": "call-current",
                "raw_payload": {
                    "manual_outbound_call_id": "another-call",
                    "call_time": "2026-10-29T10:00:00+08:00",
                },
            },
        ],
        transcript="客户：明天微信联系。",
        has_recording=True,
    )
    assert context["latest_call"]["occurred_at"] == "2026-10-28T10:00:00+08:00"
    assert context["latest_call"]["time_source_record_id"] == 1


@pytest.mark.parametrize("claim", ["上次您说要买车", "您前面比较关注智能和油耗"])
def test_fictitious_prior_conversation_is_rejected_and_can_be_corrected(claim):
    result, _ = prepare()
    output = script_output()
    output["script"]["phone_steps"][0]["content"] = claim
    with pytest.raises(ValueError, match="没有有效接触证据"):
        validate_personalized_script(output, context=result["communication_context"])
    output["script"]["phone_steps"][0]["content"] = "想先了解您选车最在意哪一项？"
    assert validate_personalized_script(output, context=result["communication_context"])


@pytest.mark.parametrize(
    "claim",
    [
        "已经帮您预留周六十点",
        "我给您留两个时间",
        "资料已经发送给您",
        "我这边已经整理好了",
    ],
)
def test_unverified_reservations_and_deliveries_are_rejected(claim):
    result, _ = prepare(transcript="客户：明天联系。", has_recording=True)
    output = script_output()
    output["script"]["wechat_script"] = claim
    with pytest.raises(ValueError, match="没有"):
        validate_personalized_script(output, context=result["communication_context"])


def test_a_future_action_is_not_misclassified_as_an_already_executed_commitment():
    result, _ = prepare()
    output = script_output()
    output["script"]["wechat_script"] = (
        "我可以先核实公户分期条件，确认后再发给您，您希望先看哪些资料？"
    )
    assert validate_personalized_script(output, context=result["communication_context"])


def test_internal_evidence_identifiers_cannot_leak_into_customer_copy():
    result, _ = prepare()
    output = script_output()
    output["script"]["wechat_script"] = "根据F12，建议您先看一下车。"
    with pytest.raises(ValueError, match="内部事实编号"):
        validate_personalized_script(output, context=result["communication_context"])


async def test_completed_test_drive_without_recording_skips_analysis_but_uses_followup(
    tmp_path,
):
    services = OfflineServices(
        [],
        profile={
            "basic_notes": {
                "viewing_progress": {"value": "已试驾", "is_locked": 1},
                "payment_method": {"value": "公户分期条件待确认"},
            }
        },
    )
    outcome, attempts, _ = await run_offline(tmp_path, services)
    assert outcome.succeeded, outcome.error
    assert outcome.node(flow.FOLLOWUP).succeeded
    assert outcome.node(flow.FIRST).status == "skipped"
    assert services.counts["script"] == 1
    assert services.counts["audit"] == services.counts["follow_up"] == 0
    assert [a["node_id"] for a in attempts if a["kind"] == "llm"] == [flow.FOLLOWUP]
    context = json.loads(outcome.node(flow.CONTEXT).outputs["communication_context"])
    assert context["facts"][0]["basis"] == "人工确认"
    result = json.loads(outcome.outputs["result"])
    assert result["audit"]["overall_rating"] == "未质检"
    assert result["follow_up_items"] == []


async def test_context_validation_errors_retry_inside_the_existing_budget(tmp_path):
    bad = script_output()
    bad["script"]["wechat_script"] = "您前面比较关注智能和油耗，这周来看看吧。"
    services = OfflineServices([], bad_script=1, bad_script_text=json.dumps(bad))
    outcome, attempts, _ = await run_offline(tmp_path, services)
    assert outcome.succeeded, outcome.error
    assert services.counts["script"] == 2
    assert services.counts["audit"] == services.counts["follow_up"] == 0
    first, second = [a for a in attempts if a["kind"] == "llm"]
    assert "没有有效接触证据" in first["anomaly"]
    assert "没有有效接触证据" in second["request_json"]["messages"][-1]["content"]


def test_audio_analysis_cannot_be_skipped_by_a_model_claim():
    with pytest.raises(ValueError, match="有录音"):
        flow.no_recording_analysis(True)

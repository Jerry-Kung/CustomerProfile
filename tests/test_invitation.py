"""邀约迁移的离线端到端验证：所有 HTTP/ASR/LLM 都走 MockTransport。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from customer_profile.invitation_contract import (
    AUDIT_ITEMS,
    PHONE_TITLES,
    validate_audit,
    validate_script,
)
from customer_profile.runner import build_service
from customer_profile.settings import Settings
from customer_profile.templates import TemplateRepository
from customer_profile.workflows import invitation as flow
from customer_profile.workflows.invitation import select_recordings

PHONE = "13800000000"
BATCH = "call-001"
EXECUTION = "execution-001"
TRANSCRIPT = "销售：您好，我是门店顾问。客户：我现在忙，周末微信发我报价。销售：好的。"


def audit_output():
    return {
        "audit": {
            "overall_score": 0,
            "overall_rating": "待计算",
            "rating_description": "客户忙碌，销售及时转微信承接。",
            "dimensions": [
                {
                    "key": key,
                    "name": name,
                    "score": score,
                    "items": [
                        {
                            "order": i,
                            "name": item,
                            "result": "pass",
                            "highlights": [],
                            "issues": [],
                        }
                        for i, item in enumerate(items, 1)
                    ],
                }
                for (key, (name, items)), score in zip(
                    AUDIT_ITEMS.items(), [80, 80, 81, 81]
                )
            ],
        }
    }


def script_output():
    return {
        "script": {
            "strategy": {
                "recommended_channel": "微信先行",
                "communication_stance": ["专业", "低压"],
                "key_entry_points": ["价格方案"],
                "avoid_words": ["暴露画像来源", "暴露录音来源"],
                "reasoning": {
                    "summary": "尊重客户的沟通安排。",
                    "steps": ["先确认客户方便的时间。", "围绕已表达的需求沟通。"],
                },
            },
            "phone_steps": [
                {"title": title, "content": f"{title}时先询问客户是否方便。"}
                for title in PHONE_TITLES
            ],
            "wechat_script": "您好，我是门店销售顾问。\n\n您看周末微信上确认价格方案方便吗？",
        }
    }


def recording(
    *urls, record_id=1, batch=BATCH, updated_at="2026-10-29T09:30:00+08:00",
    channel=flow.LEGACY_RECORDING_CHANNEL,
):
    return {
        "id": record_id,
        "channel": channel,
        "batch_id": "profile-batch-1",
        "raw_payload": {"manual_outbound_call_id": batch},
        "media_urls": list(urls),
        "updated_at": updated_at,
    }


class OfflineServices:
    def __init__(
        self,
        records,
        *,
        bad_script=0,
        bad_script_text=None,
        readback_execution=EXECUTION,
        invalid_audio=False,
        history_status=200,
        callback_timeout=False,
    ):
        self.records = records
        self.bad_script = bad_script
        self.bad_script_text = bad_script_text
        self.readback_execution = readback_execution
        self.invalid_audio = invalid_audio
        self.history_status = history_status
        self.callback_timeout = callback_timeout
        self.requests = []
        self.prompts = []
        self.counts = Counter()
        self.callbacks = []
        self.asr_urls = []

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            prompt = next(m["content"] for m in payload["messages"] if m["role"] == "user")
            self.prompts.append(prompt)
            if "判断该段录音是否为无效录音" in prompt:
                kind = "validity"
                result = {"is_valid": not self.invalid_audio, "reason": "离线样本"}
            elif "你是一位语义分析专家" in prompt:
                kind = "proofread"
                result = TRANSCRIPT
            elif "汽车销售邀约通话质检专家" in prompt:
                kind = "audit"
                result = audit_output()
            elif "汽车销售通话总结与待办事项提取器" in prompt:
                kind = "follow_up"
                result = {
                    "follow_up_summary": "客户在忙，周末微信发报价",
                    "follow_up_items": [{"order": 1, "content": "周末微信发送报价"}],
                }
            elif '"phone_steps"' in prompt and '"wechat_script"' in prompt:
                kind = "script"
                result = (
                    (self.bad_script_text if self.bad_script_text is not None else {})
                    if self.counts[kind] < self.bad_script
                    else script_output()
                )
            else:
                raise AssertionError("出现未识别的模型请求")
            self.counts[kind] += 1
            text = (
                result
                if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False)
            )
            return httpx.Response(
                200,
                json={
                    "id": f"offline-{kind}-{self.counts[kind]}",
                    "model": "offline",
                    "choices": [
                        {"message": {"content": text}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 300,
                        "total_tokens": 400,
                    },
                },
            )
        if path.endswith("/submit"):
            self.asr_urls.append(json.loads(request.content)["audio"]["url"])
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000", "X-Tt-Logid": "offline"},
                json={},
            )
        if path.endswith("/query"):
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={
                    "result": {
                        "utterances": [
                            {
                                "text": TRANSCRIPT,
                                "additions": {"speaker": "1", "emotion": "neutral"},
                            }
                        ]
                    }
                },
            )
        if path.endswith(f"/data/history/{PHONE}"):
            return httpx.Response(self.history_status, json=self.records)
        if path.endswith(f"/data/profile/{PHONE}"):
            return httpx.Response(
                200,
                json={"basic_notes": {"budget": {"value": "15万", "is_locked": True}}},
            )
        if path.endswith("/callback/update-invitation"):
            self.callbacks.append(json.loads(request.content))
            if self.callback_timeout:
                raise httpx.ReadTimeout("offline callback timeout", request=request)
            return httpx.Response(
                200, json={"msg": "Invitation analysis updated successfully."}
            )
        if path.endswith(f"/data/invitation/{PHONE}/status"):
            return httpx.Response(
                200,
                json={
                    "phone": PHONE,
                    "batch_id": BATCH,
                    "execution_id": self.readback_execution,
                    "analysis_status": 0,
                    "has_script": True,
                    "has_audit": True,
                },
            )
        raise AssertionError(f"出现未预期的请求: {request.method} {request.url}")


async def run_offline(
    tmp_path, services, *, env="test", writeback=True, inputs=None, model="offline"
):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'invitation.db'}",
        data_dir=tmp_path,
        llm_base_url="https://llm.invalid/v1",
        llm_model=model,
        llm_api_key="offline-model-secret",
        profile_api_base_url="https://test-m2m.invalid",
        profile_api_key="offline-test-secret",
        mhero_base_url="https://prod-m2m.invalid/customer_profile/m2m_api",
        profile_mhero_api_key="offline-prod-secret",
        auc_base_url="https://asr.invalid/api/v3/auc/bigmodel",
        auc_api_key="offline-asr-secret",
        auc_poll_interval_s=0,
        inline_worker=False,
        interrupt_on_start=False,
        writeback_enabled=writeback,
        http_retry_enabled=False,
        replay_mode="off",
    )
    service = await build_service(settings)
    transport = httpx.MockTransport(services.handle)
    service.http_client._transport = transport
    service.llm_client._transport = transport
    service.llm_client._backoff_seconds = lambda _: 0
    try:
        outcome = await service.run(
            flow.WORKFLOW_ID,
            inputs
            or {
                "phone_number": PHONE,
                "batch_id": BATCH,
                "work_env": env,
                "execution_id": EXECUTION,
            },
        )
        attempts = await service.store.list_attempts(outcome.run_id)
        children = await service.store.list_child_runs(outcome.run_id)
        return outcome, attempts, children
    finally:
        await service.aclose()
        service.store.close()


@pytest.mark.parametrize("env", ["test", "production"])
@pytest.mark.parametrize("file_count", [0, 1, 2])
@pytest.mark.parametrize("channel", [flow.RECORDING_CHANNEL, flow.LEGACY_RECORDING_CHANNEL])
async def test_full_workflow_branches_and_delivery(tmp_path, env, file_count, channel):
    urls = [f"https://audio.invalid/file-{i}.wav" for i in range(file_count)]
    services = OfflineServices([recording(*urls, channel=channel)])
    outcome, attempts, children = await run_offline(tmp_path, services, env=env)
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["delivery"] == "confirmed"
    assert services.asr_urls == urls
    assert services.counts["proofread"] == services.counts["validity"] == file_count
    assert (
        services.counts["script"]
        == services.counts["audit"]
        == services.counts["follow_up"]
        == 1
    )
    assert outcome.node(flow.FIRST).succeeded == (file_count == 0)
    assert outcome.node(flow.FOLLOWUP).succeeded == (file_count != 0)
    assert len(children) == 1
    result = json.loads(outcome.outputs["result"])
    assert result == services.callbacks[0]
    assert result["execution_id"] == EXECUTION
    assert result["phone"] == PHONE and result["batch_id"] == BATCH
    assert result["audit"]["overall_score"] == 81
    assert result["audit"]["overall_rating"] == "良好"
    assert result["audit"]["recording_label"] == "外呼录音 · 2026-10-29 09:30"
    assert result["generated_at"].endswith("+08:00")
    assert "{{customer_profile}}" not in "".join(services.prompts)
    assert "{{ call_transcript }}" not in "".join(services.prompts)
    script_prompts = [
        prompt for prompt in services.prompts
        if '"phone_steps"' in prompt and '"wechat_script"' in prompt
    ]
    assert len(script_prompts) == 1
    script_prompt = script_prompts[0]
    assert "猛士" in script_prompt
    assert "广汽埃安" not in script_prompt
    assert "10–15 万元" not in script_prompt
    assert "15万" in script_prompt  # 客户的真实预算仍须传入，不能替换成车辆报价。
    if file_count:
        assert "周末微信发我报价" in script_prompt
    for req in services.requests:
        if "m2m.invalid" in req.url.host:
            assert req.url.host == (
                "prod-m2m.invalid" if env == "production" else "test-m2m.invalid"
            )
            assert (
                req.headers["x-api-key"]
                == f"offline-{'prod' if env == 'production' else 'test'}-secret"
            )
    assert "offline-test-secret" not in json.dumps(attempts)
    assert "offline-prod-secret" not in json.dumps(attempts)


async def test_shadow_does_not_write_or_check_delivery(tmp_path):
    services = OfflineServices([])
    outcome, *_ = await run_offline(tmp_path, services, writeback=False)
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["delivery"] == "skipped"
    assert services.callbacks == []
    assert not any("/status" in req.url.path for req in services.requests)
    assert (
        json.loads(outcome.outputs["result"])["audit"]["recording_label"] == "外呼录音"
    )


async def test_schema_errors_retry_with_reasons_in_database(tmp_path):
    services = OfflineServices([], bad_script=1)
    outcome, attempts, _ = await run_offline(tmp_path, services)
    assert outcome.succeeded, outcome.error
    assert services.counts["script"] == 2
    script_attempts = [a for a in attempts if a["node_id"] == flow.FIRST]
    assert len(script_attempts) == 2
    assert "invalid_output_schema" in json.dumps(script_attempts[0])
    first = script_attempts[0]["request_json"]
    retry = script_attempts[1]["request_json"]
    assert len(retry["messages"]) == len(first["messages"]) + 2
    assert retry["messages"][-2] == {"role": "assistant", "content": "{}"}
    assert "script: missing" in retry["messages"][-1]["content"]


@pytest.mark.parametrize(
    ("model", "mode"),
    [("deepseek-v4.1-flash", "json_object"), ("qwen3.8-flash", "json_schema")],
)
async def test_only_invitation_object_nodes_enable_structured_output(tmp_path, model, mode):
    services = OfflineServices([recording("https://audio.invalid/a.wav", channel=flow.RECORDING_CHANNEL)])
    outcome, *_ = await run_offline(tmp_path, services, model=model)
    assert outcome.succeeded, outcome.error
    payloads = [json.loads(r.content) for r in services.requests if r.url.path.endswith("/chat/completions")]
    assert len(payloads) == 5  # 校对文本 + 有效性、质检、待办、话术四个对象。
    object_requests = [p for p in payloads if "response_format" in p]
    assert len(object_requests) == 4
    assert {p["response_format"]["type"] for p in object_requests} == {mode}
    if mode == "json_schema":
        assert {p["response_format"]["json_schema"]["name"] for p in object_requests} == {
            "AudioValidity", "AuditOutput", "FollowUpOutput", "ScriptOutput",
        }
        assert all(p["response_format"]["json_schema"]["strict"] for p in object_requests)


async def test_incomplete_json_is_retried_instead_of_repaired(tmp_path):
    services = OfflineServices(
        [], bad_script=1, bad_script_text=json.dumps(script_output())[:-1]
    )
    outcome, *_ = await run_offline(tmp_path, services)
    assert outcome.succeeded, outcome.error
    assert services.counts["script"] == 2


async def test_bad_model_output_exhausts_three_attempts_and_never_writes(tmp_path):
    services = OfflineServices([], bad_script=3)
    outcome, *_ = await run_offline(tmp_path, services)
    assert not outcome.succeeded
    assert services.counts["script"] == 3
    assert services.callbacks == []
    assert outcome.node(flow.WRITE).status == "blocked"


async def test_invalid_recording_uses_followup_branch(tmp_path):
    services = OfflineServices(
        [recording("https://audio.invalid/a.wav")], invalid_audio=True
    )
    outcome, *_ = await run_offline(tmp_path, services)
    assert outcome.succeeded, outcome.error
    assert outcome.node(flow.FOLLOWUP).succeeded
    assert any(flow.INVALID_RECORDING in p for p in services.prompts)


async def test_read_error_is_not_treated_as_missing_data(tmp_path):
    services = OfflineServices([], history_status=503)
    outcome, *_ = await run_offline(tmp_path, services)
    assert not outcome.succeeded
    assert services.callbacks == []
    assert services.counts["script"] == 0


async def test_stale_success_callback_requires_reconciliation(tmp_path):
    services = OfflineServices([], readback_execution="a-newer-execution")
    outcome, *_ = await run_offline(tmp_path, services)
    assert not outcome.succeeded
    assert "INVITATION_REQUIRES_RECONCILIATION" in outcome.error
    assert len(services.callbacks) == 1


async def test_callback_timeout_is_not_retried(tmp_path):
    services = OfflineServices([], callback_timeout=True)
    outcome, *_ = await run_offline(tmp_path, services)
    assert not outcome.succeeded
    assert len(services.callbacks) == 1


async def test_workflow_defaults_are_usable_without_optional_inputs(tmp_path):
    services = OfflineServices([])
    outcome, *_ = await run_offline(
        tmp_path,
        services,
        writeback=False,
        inputs={"phone_number": PHONE, "batch_id": BATCH},
    )
    assert outcome.succeeded, outcome.error
    assert "execution_id" not in json.loads(outcome.outputs["result"])


def test_recording_selection_prefers_the_requested_call():
    old = recording("https://audio.invalid/old.wav", record_id=1)
    new = recording("https://audio.invalid/new.wav", record_id=2, batch="call-002")
    assert (
        select_recordings(json.dumps([old, new]), BATCH)["files"] == old["media_urls"]
    )
    with pytest.raises(ValueError, match="批次"):
        select_recordings(json.dumps([old, new]), "missing")


def test_canonical_recordings_merge_only_the_requested_call_and_deduplicate_urls():
    records = [
        recording("https://audio.invalid/history.wav", batch="older-call", record_id=100),
        recording("https://audio.invalid/first.wav", channel=flow.RECORDING_CHANNEL, record_id=2),
        recording("https://audio.invalid/last.wav", "https://audio.invalid/first.wav",
                  channel=flow.RECORDING_CHANNEL, record_id=3),
        recording("https://audio.invalid/other.wav", batch="other-call", channel=flow.RECORDING_CHANNEL, record_id=4),
        {**recording("https://audio.invalid/invalid.wav", channel=flow.RECORDING_CHANNEL, record_id=5), "is_valid": 0},
        {**recording("https://audio.invalid/invalid-string.wav", channel=flow.RECORDING_CHANNEL, record_id=6), "is_valid": "0"},
        recording("https://audio.invalid/not-audio.wav", channel="OntologyManualOutboundCall", record_id=7),
    ]
    result = select_recordings(json.dumps(list(reversed(records))), BATCH)
    assert result["files"] == ["https://audio.invalid/first.wav", "https://audio.invalid/last.wav"]
    assert result["record_id"] == 3
    assert result["record_ids"] == [2, 3]


def test_canonical_details_take_precedence_over_legacy_cumulative_recording_pack():
    records = [
        recording("https://audio.invalid/current.wav", channel=flow.RECORDING_CHANNEL, record_id=1),
        recording("https://audio.invalid/current.wav", "https://audio.invalid/unrelated.wav", record_id=100),
    ]
    assert select_recordings(json.dumps(records), BATCH)["files"] == ["https://audio.invalid/current.wav"]


def test_explicit_call_match_takes_precedence_over_a_coinciding_profile_batch():
    target = recording("https://audio.invalid/target.wav", channel=flow.RECORDING_CHANNEL)
    other = recording("https://audio.invalid/wrong.wav", batch="other-call", channel=flow.RECORDING_CHANNEL, record_id=2)
    other["batch_id"] = BATCH
    assert select_recordings(json.dumps([target, other]), BATCH)["files"] == target["media_urls"]


def test_shared_profile_batch_cannot_mix_multiple_calls():
    rows = [recording("https://audio.invalid/a.wav", batch=call, channel=flow.RECORDING_CHANNEL)
            for call in ("call-1", "call-2")]
    with pytest.raises(ValueError, match="多通外呼"):
        select_recordings(json.dumps(rows), "profile-batch-1")


def test_canonical_only_batch_mismatch_is_not_treated_as_no_recording():
    rows = [recording("https://audio.invalid/a.wav", channel=flow.RECORDING_CHANNEL)]
    with pytest.raises(ValueError, match="批次"):
        select_recordings(json.dumps(rows), "missing-call")


def test_canonical_recording_without_identity_cannot_use_the_legacy_fallback():
    row = recording("https://audio.invalid/a.wav", channel=flow.RECORDING_CHANNEL)
    row.pop("batch_id")
    row.pop("raw_payload")
    with pytest.raises(ValueError, match="无法确认"):
        select_recordings(json.dumps([row]), BATCH)


def test_canonical_source_business_id_and_serialized_payload_are_supported():
    row = recording("https://audio.invalid/a.wav", channel=flow.RECORDING_CHANNEL)
    row["raw_payload"] = json.dumps({"source_business_id": BATCH})
    assert select_recordings(json.dumps([row]), BATCH)["files"] == row["media_urls"]


def test_single_call_can_be_selected_via_its_legacy_profile_batch():
    rows = [recording(f"https://audio.invalid/{i}.wav", channel=flow.RECORDING_CHANNEL, record_id=i)
            for i in (1, 2)]
    result = select_recordings(json.dumps(rows), "profile-batch-1")
    assert result["record_ids"] == [1, 2]


def test_matching_legacy_call_still_works_when_canonical_rows_belong_to_other_calls():
    legacy = recording("https://audio.invalid/legacy.wav")
    other = recording("https://audio.invalid/new.wav", batch="other-call", channel=flow.RECORDING_CHANNEL)
    assert select_recordings(json.dumps([legacy, other]), BATCH)["files"] == legacy["media_urls"]


def test_legacy_recordings_without_identity_use_the_latest_entry():
    records = [
        {
            "channel": "AIOutboundCallRecordingFile",
            "media_urls": [f"https://audio.invalid/{name}.wav"],
        }
        for name in ("old", "new")
    ]
    assert select_recordings(json.dumps(records), BATCH)["files"] == [
        "https://audio.invalid/new.wav"
    ]


@pytest.mark.parametrize(
    "urls", [[], "https://audio.invalid/single.wav", '["https://audio.invalid/a.wav"]']
)
def test_recording_url_shapes(urls):
    record = recording()
    record["media_urls"] = urls
    result = select_recordings(json.dumps([record]), BATCH)
    assert isinstance(result["files"], list)
    assert len(result["files"]) == (0 if urls == [] else 1)


@pytest.mark.parametrize("records", ['{"error":"upstream failed"}', "broken json"])
def test_malformed_history_is_rejected(records):
    with pytest.raises(ValueError):
        select_recordings(records, BATCH)


def test_audit_totals_are_computed_by_code():
    audit = validate_audit(audit_output())["audit"]
    assert audit["overall_score"] == 81 and audit["overall_rating"] == "良好"
    wrong = copy.deepcopy(audit_output())
    wrong["audit"]["dimensions"][0]["score"] = 101
    with pytest.raises(ValueError):
        validate_audit(wrong)


def test_script_contract_rejects_missing_phone_step():
    output = script_output()
    output["script"]["phone_steps"].pop()
    with pytest.raises(ValueError):
        validate_script(output)


def test_prompt_sources_and_authorized_revisions_are_available_without_downloads():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "docs/reference/invitation-workflow-sources.json").read_text()
    )
    repo = TemplateRepository()
    for entry in manifest["templates"]:
        template = repo.load(entry["template"])
        revision = entry.get("runtime_revision", {})
        expected = revision.get("sha256", entry["sha256"])
        assert hashlib.sha256(template.text.encode()).hexdigest() == expected
        if revision:
            assert revision["date"] and revision["reason"]
            assert expected != entry["sha256"]  # 保留原导出指纹，修订另记。
    assert len(manifest["sources"]) == 4 and len(manifest["templates"]) == 7
    assert {
        entry["template"] for entry in manifest["templates"]
        if entry.get("runtime_revision")
    } == {"invitation__1780473244130", "invitation__17821186748980"}

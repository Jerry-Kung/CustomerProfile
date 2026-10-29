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


def recording(*urls, record_id=1, batch=BATCH, updated_at="2026-10-29T09:30:00+08:00"):
    return {
        "id": record_id,
        "channel": "AIOutboundCallRecordingFile",
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
            prompt = payload["messages"][-1]["content"]
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


async def run_offline(tmp_path, services, *, env="test", writeback=True, inputs=None):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'invitation.db'}",
        data_dir=tmp_path,
        llm_base_url="https://llm.invalid/v1",
        llm_model="offline",
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
async def test_full_workflow_branches_and_delivery(tmp_path, env, file_count):
    urls = [f"https://audio.invalid/file-{i}.wav" for i in range(file_count)]
    services = OfflineServices([recording(*urls)])
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


def test_prompt_sources_are_frozen_and_available_without_downloads():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "docs/reference/invitation-workflow-sources.json").read_text()
    )
    repo = TemplateRepository()
    for entry in manifest["templates"]:
        template = repo.load(entry["template"])
        assert hashlib.sha256(template.text.encode()).hexdigest() == entry["sha256"]
    assert len(manifest["sources"]) == 4 and len(manifest["templates"]) == 7

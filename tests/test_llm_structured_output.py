"""结构化输出的实际协议、纠错请求与三次尝试边界；不调用真实模型。"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from customer_profile.execution.llm import LlmCallError, LlmClient
from customer_profile.execution.quota import estimate_prompt_tokens
from customer_profile.invitation_contract import output_schema, validate_audio_validity
from customer_profile.replay import llm_fingerprint

from .conftest import make_settings

VALID = '{"is_valid": true, "reason": "双方有效沟通"}'


def client_for(tmp_path, frames, *, model="deepseek-v4.1-flash"):
    requests, attempts = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        status, text = frames[min(len(requests) - 1, len(frames) - 1)]
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": text}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            },
        )

    async def record(ref, attempt, payload):
        attempts.append(attempt.asdict())

    client = LlmClient(
        make_settings(tmp_path, _env_file=None, llm_model=model),
        transport=httpx.MockTransport(respond),
        recorder=record,
    )
    client._backoff_seconds = lambda _: 0
    return client, requests, attempts


async def call(client):
    return await client.llm_call(
        "请判断原始通话是否有效。",
        expect_json=True,
        node_ref=object(),
        response_validator=validate_audio_validity,
        response_schema=output_schema("validate_audio_validity"),
    )


@pytest.mark.parametrize("model", ["qwen3.8-flash", "qwen3.8-flash-20261001"])
async def test_qwen_flash_uses_schema_derived_from_the_local_contract(tmp_path, model):
    client, requests, _ = client_for(tmp_path, [(200, VALID)], model=model)
    try:
        assert await call(client) == json.loads(VALID)
    finally:
        await client.aclose()
    response_format = requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["is_valid"]["type"] == "boolean"
    assert set(schema["required"]) == {"is_valid", "reason"}


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "qwen-flash", "custom-model"])
async def test_models_without_confirmed_schema_capability_use_json_object(
    tmp_path, model
):
    client, requests, _ = client_for(tmp_path, [(200, VALID)], model=model)
    try:
        await call(client)
    finally:
        await client.aclose()
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "JSON" in requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (VALID[:-1], "invalid_json"),
        ('{"is_valid":"true","reason":"通话"}', "is_valid: bool_type"),
    ],
)
async def test_retry_carries_parse_or_schema_error_and_preserves_original_attempt(
    tmp_path, bad, reason
):
    client, requests, attempts = client_for(tmp_path, [(200, bad), (200, VALID)])
    try:
        assert await call(client) == json.loads(VALID)
    finally:
        await client.aclose()
    assert len(requests) == len(attempts) == 2
    assert requests[1]["messages"][:-2] == requests[0]["messages"]
    assert requests[1]["messages"][-2] == {"role": "assistant", "content": bad}
    assert reason in requests[1]["messages"][-1]["content"]
    assert attempts[0]["response_text"] == bad
    assert attempts[0]["request"] == requests[0]
    assert attempts[1]["request"] == requests[1]


async def test_repeated_bad_outputs_are_not_repaired_or_accepted_and_stop_at_three(
    tmp_path,
):
    client, requests, attempts = client_for(tmp_path, [(200, VALID[:-1])])
    try:
        with pytest.raises(LlmCallError) as exc:
            await call(client)
    finally:
        await client.aclose()
    assert len(requests) == len(attempts) == len(exc.value.attempts) == 3
    assert all(a["response_text"] == VALID[:-1] for a in attempts)
    assert len(requests[2]["messages"]) == len(requests[1]["messages"])


@pytest.mark.parametrize("status", [400, 422])
async def test_proxy_explicitly_rejecting_schema_falls_back_in_the_same_budget(
    tmp_path, status
):
    client, requests, attempts = client_for(
        tmp_path,
        [
            (status, "response_format json_schema is not supported"),
            (200, VALID),
        ],
        model="qwen3.8-flash",
    )
    try:
        assert await call(client) == json.loads(VALID)
    finally:
        await client.aclose()
    assert [p["response_format"]["type"] for p in requests] == [
        "json_schema",
        "json_object",
    ]
    assert attempts[0]["error_code"] == f"http_{status}"


async def test_protocol_fallback_does_not_reset_the_three_attempt_budget(tmp_path):
    client, requests, _ = client_for(
        tmp_path,
        [
            (400, "json_schema is not supported"),
            (200, "{}"),
        ],
        model="qwen3.8-flash",
    )
    try:
        with pytest.raises(LlmCallError) as exc:
            await call(client)
    finally:
        await client.aclose()
    assert len(requests) == len(exc.value.attempts) == 3
    assert [p["response_format"]["type"] for p in requests] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_auth_rate_and_server_errors_do_not_downgrade_schema(tmp_path, status):
    client, requests, _ = client_for(
        tmp_path,
        [
            (status, "json_schema unsupported"),
            (200, VALID),
        ],
        model="qwen3.8-flash",
    )
    try:
        await call(client)
    finally:
        await client.aclose()
    assert requests[0] == requests[1]
    assert requests[1]["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize("kind", ["text", "array", "jsonl"])
async def test_plain_text_arrays_and_jsonl_do_not_get_an_object_response_format(
    tmp_path, kind
):
    text = {"text": "普通回答", "array": '[{"id":1}]', "jsonl": '{"id":1}\n{"id":2}'}[
        kind
    ]
    client, requests, _ = client_for(tmp_path, [(200, text)], model="qwen3.8-flash")
    try:
        await client.llm_call(
            "原始提示词", expect_json=kind == "array", expect_jsonl=kind == "jsonl"
        )
    finally:
        await client.aclose()
    assert "response_format" not in requests[0]


async def test_schema_cannot_be_applied_to_jsonl(tmp_path):
    client, requests, _ = client_for(tmp_path, [(200, VALID)])
    try:
        with pytest.raises(ValueError, match="JSON 对象"):
            await client.llm_call(
                "提示词",
                expect_json=True,
                expect_jsonl=True,
                response_validator=validate_audio_validity,
                response_schema=output_schema("validate_audio_validity"),
            )
    finally:
        await client.aclose()
    assert not requests


def test_replay_does_not_reuse_responses_from_a_different_output_contract():
    request = {
        "model": "qwen3.8-flash",
        "messages": [{"role": "user", "content": "判断"}],
    }
    historical = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert llm_fingerprint(request) == historical
    object_request = {**request, "response_format": {"type": "json_object"}}
    schema_request = {
        **request,
        "response_format": {
            "type": "json_schema",
            "json_schema": output_schema("validate_audio_validity"),
        },
    }
    assert (
        len({llm_fingerprint(p) for p in (request, object_request, schema_request)})
        == 3
    )
    changed_schema = {
        **request,
        "response_format": {
            "type": "json_schema",
            "json_schema": output_schema("validate_script"),
        },
    }
    assert llm_fingerprint(schema_request) != llm_fingerprint(changed_schema)
    assert estimate_prompt_tokens(schema_request) > estimate_prompt_tokens(
        object_request
    )

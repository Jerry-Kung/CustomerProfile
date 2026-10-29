"""模型配置必须影响实际请求，且不能错用其他识别参数下的缓存。"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from pydantic import ValidationError

from customer_profile.execution.auc import OK, AucClient, build_submit_body
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.transcript_cache import TranscriptCache
from customer_profile.settings import Settings


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    for key in os.environ:
        if key.upper().startswith(("AUC_", "LLM_")):
            monkeypatch.delenv(key)


def test_missing_asr_configuration_keeps_existing_payload():
    settings = Settings(_env_file=None)
    body = build_submit_body("https://audio.example.invalid/a.wav", settings)
    assert body["request"] == {
        "model_name": "bigmodel",
        "model_version": "400",
        "ssd_version": "200",
        "enable_channel_split": True,
        "enable_ddc": True,
        "enable_speaker_info": True,
        "enable_punc": True,
        "enable_itn": True,
        "show_speech_rate": True,
        "show_volume": True,
        "enable_emotion_detection": True,
        "enable_gender_detection": True,
        "corpus": {"correct_table_name": "", "context": ""},
    }
    assert settings.auc_submit_path == "/submit"
    assert settings.auc_query_path == "/query"
    assert settings.llm_chat_completions_path == "/chat/completions"


async def test_asr_env_file_controls_actual_submission_and_query(tmp_path, monkeypatch):
    config = tmp_path / ".env"
    config.write_text(
        "AUC_BASE_URL=https://asr.example.invalid/gateway/v1\n"
        "AUC_SUBMIT_PATH=/jobs/create\n"
        "AUC_QUERY_PATH=/jobs/status\n"
        "AUC_MODEL_NAME=custom-model\n"
        "AUC_MODEL_VERSION=500\n"
        "AUC_SSD_VERSION=300\n"
        "AUC_ENABLE_CHANNEL_SPLIT=false\n"
        "AUC_ENABLE_DDC=false\n"
        "AUC_ENABLE_SPEAKER_INFO=false\n"
        "AUC_ENABLE_PUNC=false\n"
        "AUC_ENABLE_ITN=false\n"
        "AUC_SHOW_SPEECH_RATE=false\n"
        "AUC_SHOW_VOLUME=false\n"
        "AUC_ENABLE_EMOTION_DETECTION=false\n"
        "AUC_ENABLE_GENDER_DETECTION=false\n"
        "AUC_CORPUS_CORRECT_TABLE_NAME=car-terms\n"
        "AUC_CORPUS_CONTEXT=预约试驾\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUC_MODEL_VERSION", "600")
    settings = Settings(_env_file=config)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"X-Api-Status-Code": OK},
            json={"result": {"utterances": []}},
        )

    client = AucClient(settings, transport=httpx.MockTransport(respond))
    try:
        submit = await client.request(
            "POST",
            "/submit_analyze",
            json_body={"file_url": "https://audio.invalid/a.wav"},
        )
        query = await client.request("POST", "/query_analyze", json_body={})
    finally:
        await client.aclose()
    assert submit.succeeded, submit.error
    assert query.succeeded, query.error
    assert [req.url.path for req in requests] == [
        "/gateway/v1/jobs/create",
        "/gateway/v1/jobs/status",
    ]
    params = json.loads(requests[0].content)["request"]
    assert params["model_name"] == "custom-model"
    assert params["model_version"] == "600", "环境变量应覆盖配置文件"
    assert params["ssd_version"] == "300"
    assert all(
        value is False
        for key, value in params.items()
        if key.startswith(("enable_", "show_"))
    )
    assert params["corpus"] == {
        "correct_table_name": "car-terms",
        "context": "预约试驾",
    }


def test_blank_asr_versions_are_not_sent(tmp_path):
    config = tmp_path / ".env"
    config.write_text("AUC_MODEL_VERSION=\nAUC_SSD_VERSION=\n", encoding="utf-8")
    params = build_submit_body(
        "https://audio.invalid/a.wav", Settings(_env_file=config)
    )["request"]
    assert "model_version" not in params
    assert "ssd_version" not in params


@pytest.mark.parametrize(
    "overrides", [{"auc_model_version": "500"}, {"auc_enable_punc": False}]
)
async def test_configured_asr_parameters_do_not_reuse_old_transcripts(
    tmp_path, overrides
):
    cache = TranscriptCache(tmp_path / "cache")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"X-Api-Status-Code": OK},
            json={"result": {"utterances": []}},
        )

    for options in ({}, overrides):
        client = AucClient(
            Settings(_env_file=None, **options),
            transport=httpx.MockTransport(respond),
            transcript_cache=cache,
        )
        try:
            submit = await client.request(
                "POST",
                "/submit_analyze",
                json_body={"file_url": "https://audio.invalid/a.wav"},
            )
            query = await client.request("POST", "/query_analyze", json_body={})
        finally:
            await client.aclose()
        assert submit.succeeded and not submit.replayed
        assert query.succeeded and not query.replayed
    assert len(requests) == 4


async def test_llm_env_file_controls_actual_model_and_api_path(tmp_path):
    config = tmp_path / ".env"
    config.write_text(
        "LLM_BASE_URL=https://llm.example.invalid/proxy/v1\n"
        "LLM_MODEL=custom-text-model\n"
        "LLM_CHAT_COMPLETIONS_PATH=/deploy/chat\n",
        encoding="utf-8",
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "正常回答"}, "finish_reason": "stop"}
                ]
            },
        )

    client = LlmClient(
        Settings(_env_file=config), transport=httpx.MockTransport(respond)
    )
    try:
        assert await client.llm_call("测试") == "正常回答"
    finally:
        await client.aclose()
    assert requests[0].url.path == "/proxy/v1/deploy/chat"
    assert json.loads(requests[0].content)["model"] == "custom-text-model"


@pytest.mark.parametrize(
    "field", ["llm_chat_completions_path", "auc_submit_path", "auc_query_path"]
)
@pytest.mark.parametrize(
    "path", ["", "submit", "https://other.invalid/submit", "//other.invalid/submit"]
)
def test_api_paths_cannot_override_configured_host(field, path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: path})


def test_asr_model_name_cannot_be_empty():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auc_model_name="  ")

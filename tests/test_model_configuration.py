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
    "base_url,thinking_format,expected_key",
    [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", None, "enable_thinking"),
        ("https://llm.example.cn-beijing.maas.aliyuncs.com/v1", None, "enable_thinking"),
        ("https://ark.cn-beijing.volces.com/api/v3", None, "thinking"),
        ("https://ARK.CN-SHANGHAI.VOLCES.COM/api/v3", "auto", "thinking"),
        ("https://ark.cn-beijing.volces.com.proxy.invalid/v1", None, "enable_thinking"),
        ("https://gateway.invalid/ark.cn-beijing.volces.com", None, "enable_thinking"),
        ("https://gateway.invalid/v1", "thinking", "thinking"),
        ("https://ark.cn-beijing.volces.com/api/v3", "enable_thinking", "enable_thinking"),
    ],
)
@pytest.mark.parametrize("enabled", [False, True, None])
async def test_llm_env_thinking_switch_reaches_provider(
    tmp_path, base_url, thinking_format, expected_key, enabled
):
    """从 .env 到实际 HTTP 请求验证，模型同名也不能混用两家供应商的开关。"""
    config = tmp_path / ".env"
    lines = [
        f"LLM_BASE_URL={base_url}",
        "LLM_MODEL=deepseek-v4-1-flash-261017",
        f"LLM_ENABLE_THINKING={'' if enabled is None else str(enabled).lower()}",
        "LLM_MAX_TOKENS=128000",
    ]
    if thinking_format is not None:
        lines.append(f"LLM_THINKING_FORMAT={thinking_format}")
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "正常回答"}, "finish_reason": "stop"}]},
        )

    client = LlmClient(Settings(_env_file=config), transport=httpx.MockTransport(respond))
    try:
        assert await client.llm_call("测试", max_attempts=1) == "正常回答"
    finally:
        await client.aclose()

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    if enabled is None:
        assert "thinking" not in payload
        assert "enable_thinking" not in payload
    elif expected_key == "thinking":
        assert payload["thinking"] == {"type": "enabled" if enabled else "disabled"}
        assert "enable_thinking" not in payload
    else:
        assert payload["enable_thinking"] is enabled
        assert "thinking" not in payload
    assert payload["max_tokens"] == 128000
    assert payload["temperature"] == 0.5


@pytest.mark.parametrize(
    "raw,expected", [("", "auto"), ("  ", "auto"), (" THINKING ", "thinking"), ("AUTO", "auto")]
)
def test_llm_thinking_format_normalizes_configuration(raw, expected):
    assert Settings(_env_file=None, llm_thinking_format=raw).llm_thinking_format == expected


def test_llm_thinking_format_rejects_unknown_protocol():
    with pytest.raises(ValidationError, match="llm_thinking_format"):
        Settings(_env_file=None, llm_thinking_format="volcano")


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


# ---------------------------------------------------------------- 留痕留存档位


def test_trace_persist_mode_defaults_to_high():
    settings = Settings(_env_file=None)
    assert settings.trace_persist_mode == "high"
    assert settings.trace_archive_compress_level == 6
    assert settings.trace_retention_days == 30


def test_trace_persist_mode_accepts_case_and_whitespace():
    for raw in ("LOW", " low ", "High"):
        settings = Settings(_env_file=None, trace_persist_mode=raw)
        assert settings.trace_persist_mode == raw.strip().lower()


@pytest.mark.parametrize("mode", ["none", "NONE", "medium", "off"])
def test_trace_persist_mode_rejects_unknown_values(mode):
    """不静默回落：写错档位却照常启动，是最难查的一类配置问题。"""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, trace_persist_mode=mode)
    message = str(excinfo.value)
    assert "high" in message and "low" in message


def test_legacy_boolean_flag_maps_to_new_modes():
    off = Settings(_env_file=None, trace_persist_enabled="false")
    on = Settings(_env_file=None, trace_persist_enabled="true")
    assert off.trace_persist_mode == "low"
    assert on.trace_persist_mode == "high"


def test_explicit_mode_wins_over_legacy_flag():
    settings = Settings(_env_file=None, trace_persist_enabled="false", trace_persist_mode="high")
    assert settings.trace_persist_mode == "high"


def test_archive_dir_defaults_under_data_dir(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.trace_archive_path == tmp_path / "trace-archives"
    explicit = Settings(_env_file=None, data_dir=tmp_path, trace_archive_dir=tmp_path / "x")
    assert explicit.trace_archive_path == tmp_path / "x"


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_blank_archive_dir_from_env_file_stays_under_data_dir(tmp_path, blank):
    env_file = tmp_path / "archive.env"
    env_file.write_text(f'TRACE_ARCHIVE_DIR="{blank}"\n', encoding="utf-8")
    settings = Settings(_env_file=env_file, data_dir=tmp_path / "data")
    assert settings.trace_archive_path == tmp_path / "data" / "trace-archives"


def test_ensure_dirs_creates_archive_dir(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "d")
    settings.ensure_dirs()
    assert (tmp_path / "d" / "trace-archives").is_dir()

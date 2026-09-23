"""统一 LLM 调用层测试：尝试次数、异常判定、逐次留痕。

异常判定的四条规则直接来自原 DSL「Gemini（异常输出重试版） - 判断结果是否异常」
节点代码，是 §6.4.2 指定的唯一权威依据。

刻意覆盖的两处「不能猜」：
- ``usage`` 缺失时必须**不**触发 ``usage_zero``——把「未知」当成 0 是伪造；
- ``expect_json`` 解析失败要重试，且判据来自调用点声明而非 DSL 的 ``output_json``。
"""

from __future__ import annotations

import pytest

from customer_profile.execution.llm import (
    LlmCallError,
    LlmClient,
    Usage,
    detect_output_anomaly,
    parse_json_output,
    strip_code_fence,
)
from customer_profile.replay import ReplaySource, llm_fingerprint
from customer_profile.settings import Settings

from .conftest import make_settings


# ---------------------------------------------------------------- 异常判定


def test_usage_zero_is_anomalous():
    assert detect_output_anomaly("正常文本", None, Usage(0, 0, 0)) == "usage_zero"


def test_missing_usage_is_not_treated_as_zero():
    """usage 三项皆 None 表示未知，不能判为异常。"""
    assert detect_output_anomaly("正常文本", None, Usage()) is None
    assert detect_output_anomaly("正常文本", None, None) is None


def test_partial_zero_usage_is_not_anomalous():
    assert detect_output_anomaly("正常文本", None, Usage(10, 0, 10)) is None


def test_empty_text_is_anomalous():
    assert detect_output_anomaly("   \n  ", None, Usage(1, 1, 2)) == "empty_text"
    assert detect_output_anomaly(None, None, Usage(1, 1, 2)) == "empty_text"


def test_unfinished_think_block_is_anomalous():
    text = "<think>开始思考但没有闭合"
    assert detect_output_anomaly(text, None, Usage(1, 1, 2)) == "unfinished_think_block"


def test_closed_think_block_is_not_anomalous():
    text = "<think>想完了</think>这是正式回答，长度足够不触发仅思考的怀疑。"
    assert detect_output_anomaly(text, "reasoning", Usage(1, 1, 2)) is None


def test_thinking_only_suspected():
    text = "<think>很短</think>"
    assert (
        detect_output_anomaly(text, "", Usage(1, 1, 2)) == "thinking_only_suspected"
    )


def test_thinking_only_suspected_not_triggered_when_long_enough():
    text = "<think>" + "内容" * 300 + "</think>x"
    assert detect_output_anomaly(text, "", Usage(1, 1, 2)) is None


def test_thinking_only_suspected_not_triggered_with_reasoning_content():
    text = "<think>很短</think>"
    assert detect_output_anomaly(text, "有推理内容", Usage(1, 1, 2)) is None


def test_normal_output_is_clean():
    assert detect_output_anomaly("正常回答", None, Usage(5, 5, 10)) is None


# ---------------------------------------------------------------- JSON 解析


def test_strip_code_fence_handles_json_and_plain():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_parse_json_output_accepts_fenced_payload():
    assert parse_json_output('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_output_rejects_garbage():
    with pytest.raises(ValueError):
        parse_json_output("这不是 JSON")


def test_parse_json_output_rejects_empty():
    with pytest.raises(ValueError):
        parse_json_output("   ")


# ---------------------------------------------------------------- 重试


class _Ref:
    """最小可用的节点引用。``llm_call`` 只在给了引用时才写尝试记录。"""

    run_id = "test-run"
    node_id = "test-node"
    call_path = "/"

    def asdict(self) -> dict:
        return {"run_id": self.run_id, "node_id": self.node_id, "call_path": self.call_path}


REF = _Ref()


def _client(tmp_path, frames: dict, **overrides):
    settings = make_settings(tmp_path, **overrides)
    replay = ReplaySource(llm=frames, strict=True)
    recorded: list = []

    async def recorder(node_ref, attempt, payload):
        recorded.append(attempt)

    client = LlmClient(settings, replay=replay, recorder=recorder)
    return client, replay, recorded


def _frames_for(settings, responses):
    """按实际请求体算出 fixture 键，避免测试里手写指纹。"""
    payload = client_payload(settings)
    return {llm_fingerprint(payload): responses}


def client_payload(settings: Settings) -> dict:
    return {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": "提示词"}],
        "temperature": settings.llm_temperature,
    }


async def test_single_attempt_when_output_is_healthy(tmp_path):
    frames = _frames_for(
        make_settings(tmp_path),
        [{"text": "好结果", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}],
    )
    client, _, recorded = _client(tmp_path, frames)

    result = await client.llm_call("提示词", node_ref=REF)
    assert result == "好结果"
    assert len(recorded) == 1
    assert recorded[0].attempt_no == 1
    assert recorded[0].replayed is True


async def test_retries_on_anomalous_output_then_succeeds(tmp_path):
    """第一次返回空文本（异常），第二次正常——应重试并最终成功。"""
    frames = _frames_for(
        make_settings(tmp_path),
        [
            {"text": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            {"text": "第二次成功", "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}},
        ],
    )
    client, _, recorded = _client(tmp_path, frames)

    result = await client.llm_call("提示词", node_ref=REF)
    assert result == "第二次成功"
    assert [a.attempt_no for a in recorded] == [1, 2]
    assert recorded[0].anomaly == "usage_zero"


async def test_gives_up_after_three_attempts(tmp_path):
    """最多 3 次尝试（首次 + 2 次重试），第 4 次不再发起（差异清单 M1 缺省口径）。"""
    frames = _frames_for(make_settings(tmp_path), [{"text": ""}])
    client, _, recorded = _client(tmp_path, frames)

    with pytest.raises(LlmCallError) as excinfo:
        await client.llm_call("提示词", node_ref=REF)

    assert len(excinfo.value.attempts) == 3
    assert [a.attempt_no for a in recorded] == [1, 2, 3]


async def test_max_attempts_is_configurable(tmp_path):
    frames = _frames_for(make_settings(tmp_path), [{"text": ""}])
    client, _, recorded = _client(tmp_path, frames)

    with pytest.raises(LlmCallError) as excinfo:
        await client.llm_call("提示词", max_attempts=2, node_ref=REF)
    assert len(excinfo.value.attempts) == 2
    assert len(recorded) == 2


async def test_expect_json_retries_on_invalid_json(tmp_path):
    """需要 JSON 的任务返回非 JSON 时重试；第二次给合法 JSON 即成功。"""
    frames = _frames_for(
        make_settings(tmp_path),
        [
            {"text": "这不是 JSON", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            {"text": '{"ok": true}', "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ],
    )
    client, _, recorded = _client(tmp_path, frames)

    result = await client.llm_call("提示词", expect_json=True, node_ref=REF)
    assert result == {"ok": True}
    assert len(recorded) == 2


async def test_json_fence_is_accepted(tmp_path):
    frames = _frames_for(
        make_settings(tmp_path),
        [{"text": '```json\n{"ok": 1}\n```', "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}],
    )
    client, _, _ = _client(tmp_path, frames)
    assert await client.llm_call("提示词", expect_json=True, node_ref=REF) == {"ok": 1}


async def test_replay_miss_fails_loudly_in_strict_mode(tmp_path):
    """严格模式下未命中固定响应必须失败，不能让「没跑通」看起来像「跑通了」。"""
    client, _, _ = _client(tmp_path, {"deadbeef": [{"text": "不匹配"}]})
    with pytest.raises(LlmCallError):
        await client.llm_call("提示词", node_ref=REF)


async def test_temperature_is_forced_to_unified_value(tmp_path):
    """temperature 全项目统一 0.5，且不由调用点决定（§6.4.4）。"""
    settings = make_settings(tmp_path)
    assert settings.llm_temperature == 0.5
    with pytest.raises(ValueError, match="0.5"):
        Settings(**{**settings.model_dump(), "llm_temperature": 0.7})


async def test_payload_sets_no_extra_generation_params(tmp_path):
    """除模型、messages、temperature 以外不得设置其他参数。"""
    client, _, _ = _client(tmp_path, {})
    payload = client.build_payload("提示词")
    assert set(payload) == {"model", "messages", "temperature"}


async def test_images_produce_multimodal_content(tmp_path):
    """图片输入要转成多模态 content 数组，而不是塞进纯文本。"""
    client, _, _ = _client(tmp_path, {})
    payload = client.build_payload("看图", images=["http://example/a.png"])
    content = payload["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"


async def test_system_prompt_precedes_user_message(tmp_path):
    client, _, _ = _client(tmp_path, {})
    messages = client.build_messages("正文", system="你是助手")
    assert messages[0] == {"role": "system", "content": "你是助手"}
    assert messages[1]["role"] == "user"

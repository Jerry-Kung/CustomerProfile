"""AUC 云服务适配层：把云形状翻译回内网契约。

**这一层要守住的东西。** 工作流的图与两个 HTTP 节点、两个 code 节点都不改——它们
逐字符来自 DSL，由 ``tests/test_code_verbatim.py`` 用 AST 钉住。而云服务的形状是
另一套（task_id 由调用方生成、状态在响应头、需自行轮询），所以适配层必须让那两个
code 节点**看到与内网服务一直完全一样的响应体**：

- ``/submit_analyze`` → ``{"task_id": ..., "x_tt_logid": ...}``
- ``/query_analyze``  → ``{"auc_result": ..., "poll_count": ...}``

本文件的用例就是钉住这两个形状。它们不测「云服务好不好用」（那要真实调用），
测的是「翻译对不对」——真实调用验证不了这个，因为真实调用只告诉你端到端成不成。

另有一条同样重要的用例：``parse_auc_result`` 的输出字段必须与校对提示词的示例一致。
提示词里按 ``emotion`` / ``gender`` / ``speaker`` / ``speech_rate`` / ``volume`` / ``text``
六个字段写示例，多一个少一个都会让模型对不齐示例。
"""

from __future__ import annotations

import json

import httpx
import pytest

from customer_profile.execution.auc import (
    OK,
    SILENT_AUDIO,
    AucClient,
    build_submit_body,
    parse_auc_result,
)
from customer_profile.settings import Settings

from .conftest import make_settings

SUBMIT = "/submit_analyze"
QUERY = "/query_analyze"


class _StubTransport(httpx.AsyncBaseTransport):
    """按云服务的真实协议应答：状态码全在响应头里。

    ``_StubTransport`` 而不是 ``MockTransport`` 的子类，是为了把「第几次查询返回
    处理中」这样的序列写清楚——轮询是本层最容易写错的地方。
    """

    def __init__(self, *, poll_until_done: int = 1, utterances: list | None = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._query_count = 0
        self._poll_until_done = poll_until_done
        self._utterances = utterances if utterances is not None else [
            {
                "text": "喂，你好。",
                "additions": {
                    "emotion": "neutral",
                    "gender": "female",
                    "speaker": "1",
                    "speech_rate": "4.41",
                    "volume": "73.04",
                },
            }
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        path = request.url.path
        self.requests.append((path, body))

        if path.endswith("/submit"):
            return httpx.Response(
                200,
                headers={
                    "X-Api-Status-Code": OK,
                    "X-Tt-Logid": "LOGID-PROBE-123",
                },
                json={},
            )

        assert path.endswith("/query")
        self._query_count += 1
        if self._query_count < self._poll_until_done:
            return httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000001", "X-Api-Message": "in progress"},
                json={},
            )
        return httpx.Response(
            200,
            headers={"X-Api-Status-Code": OK},
            json={"result": {"utterances": self._utterances}},
        )


class _SilentTransport(httpx.AsyncBaseTransport):
    """静音音频：状态码 20000003，服务端没有 utterances。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, headers={"X-Api-Status-Code": OK}, json={})
        return httpx.Response(
            200, headers={"X-Api-Status-Code": SILENT_AUDIO}, json={}
        )


def _client(tmp_path, transport, **overrides) -> AucClient:
    settings: Settings = make_settings(
        tmp_path, auc_api_key="probe-key", auc_resource_id="volc.seedasr.auc", **overrides
    )
    return AucClient(settings, transport=transport)


async def test_submit_is_translated_to_the_internal_contract(tmp_path):
    """``/submit_analyze`` 必须产出 ``{task_id, x_tt_logid}``，供 ``parse_task_params`` 消费。

    ``task_id`` 是**我们**生成的 UUID（云服务不回传），``x_tt_logid`` 来自响应头
    ``X-Tt-Logid``。这两点是适配层存在的理由，因此在这里钉死。
    """
    transport = _StubTransport()
    client = _client(tmp_path, transport)

    attempt = await client.request(
        "POST", SUBMIT, json_body={"file_url": "https://example.invalid/a.wav"}
    )
    assert attempt.succeeded, attempt.error

    payload = json.loads(attempt.response_text)
    assert set(payload) == {"task_id", "x_tt_logid"}, (
        f"提交响应体形状必须与内网契约一致，实际 {sorted(payload)}"
    )
    assert payload["task_id"], "task_id 不能为空——查询节点要用它"
    assert payload["x_tt_logid"] == "LOGID-PROBE-123", "logid 应取响应头的 X-Tt-Logid"

    # 发给云服务的路径是 /submit，请求体是云服务的字段形状
    path, body = transport.requests[0]
    assert path.endswith("/submit")
    assert body["audio"]["url"] == "https://example.invalid/a.wav"
    assert body["request"]["model_name"] == "bigmodel"


async def test_query_polls_until_done_and_returns_auc_result(tmp_path):
    """``/query_analyze`` 必须产出 ``{auc_result, poll_count}``，供 ``extract_auc_result`` 消费。

    轮询发生在**这一次调用内部**：返回时结果必须已经就绪——这是与内网服务
    「调用方自己反复查询」的实质差异，也是本层最该被钉住的行为。
    """
    transport = _StubTransport(poll_until_done=3)
    client = _client(tmp_path, transport)

    await client.request("POST", SUBMIT, json_body={"file_url": "https://x/a.wav"})
    attempt = await client.request("POST", QUERY, json_body={})

    assert attempt.succeeded, attempt.error
    payload = json.loads(attempt.response_text)
    assert set(payload) == {"auc_result", "poll_count"}
    assert payload["poll_count"] == 3, f"应轮询 3 次，实际 {payload['poll_count']}"

    # auc_result 是「文本化的数组」——下游把它直接插进提示词，因此必须是字符串
    assert isinstance(payload["auc_result"], str)
    parsed = json.loads(payload["auc_result"])
    assert len(parsed) == 1
    assert parsed[0]["text"] == "喂，你好。"
    # 数值字段被转成 float 并保留两位（校对提示词的示例就是这么写的）
    assert parsed[0]["speech_rate"] == 4.41
    assert parsed[0]["volume"] == 73.04


async def test_silent_audio_is_a_normal_conclusion_not_a_failure(tmp_path):
    """静音音频（20000003）算**业务结论**，不算故障——原 handler 也这么处理。

    下游据此走「该录音无有效内容」分支，因此这里必须给一个可解析的空数组，
    而不是把运行判成失败。
    """
    client = _client(tmp_path, _SilentTransport())

    await client.request("POST", SUBMIT, json_body={"file_url": "https://x/a.wav"})
    attempt = await client.request("POST", QUERY, json_body={})

    assert attempt.succeeded, attempt.error
    assert json.loads(json.loads(attempt.response_text)["auc_result"]) == []


async def test_query_without_submit_fails_loudly(tmp_path):
    """没有先提交就查询：明确报错，而不是拿空 task_id 去换一个更难懂的 45000000。"""
    client = _client(tmp_path, _StubTransport())

    attempt = await client.request("POST", QUERY, json_body={})

    assert not attempt.succeeded
    assert "找不到本次运行提交的 task_id" in (attempt.error or "")


async def test_unknown_path_is_rejected(tmp_path):
    """未知路径不静默走空——静默会让「没跑通」看起来像「跑通了」。"""
    client = _client(tmp_path, _StubTransport())

    attempt = await client.request("POST", "/not_an_auc_path", json_body={})

    assert not attempt.succeeded
    assert "未知的 AUC 路径" in (attempt.error or "")


async def test_credentials_are_masked_in_the_attempt_record(tmp_path):
    """凭据永不落痕：写进 attempt 的请求头必须是掩码后的（§6）。"""
    client = _client(tmp_path, _StubTransport())

    attempt = await client.request(
        "POST", SUBMIT, json_body={"file_url": "https://x/a.wav"}
    )

    assert attempt.request_headers.get("x-api-key") not in (None, "probe-key"), (
        f"x-api-key 应被掩码，实际 {attempt.request_headers.get('x-api-key')!r}"
    )


def test_parse_auc_result_fields_match_the_prompt_example():
    """输出字段必须与校对提示词的示例一致（六个字段，一个不多一个不少）。

    提示词按这六个字段写示例；字段漂移会让模型对不齐示例，属于静默的质量退化。
    """
    raw = json.dumps(
        {
            "result": {
                "utterances": [
                    {"text": "喂，你好。", "additions": {"emotion": "neutral"}}
                ]
            }
        }
    )
    parsed = json.loads(parse_auc_result(raw))

    assert set(parsed[0]) == {
        "emotion",
        "gender",
        "speaker",
        "speech_rate",
        "volume",
        "text",
    }, f"字段集合漂移：{sorted(parsed[0])}"


def test_submit_body_format_follows_the_extension():
    """格式按扩展名推断，与原 handler 规则一致（``.mp3`` 之外一律 wav）。"""
    assert build_submit_body("https://x/a.mp3")["audio"]["format"] == "mp3"
    assert build_submit_body("https://x/a.MP3")["audio"]["format"] == "mp3"
    assert build_submit_body("https://x/a.wav")["audio"]["format"] == "wav"
    assert build_submit_body("https://x/a.m4a")["audio"]["format"] == "wav"


async def test_audio_workflow_submit_node_is_routed_to_the_cloud_client(tmp_path):
    """真实定义的 AUC 提交节点经 ``HttpClient`` 时确实被转交给云实现。

    这条用例防的是「适配层写好了但没接上」：若 ``HttpClient.request`` 的分发被改坏，
    节点会走普通 HTTP 路径去撞 ``192.168.0.5``，而本用例会红。
    """
    from customer_profile.execution.http import HttpClient

    transport = _StubTransport()
    settings = make_settings(tmp_path, auc_api_key="probe-key")
    http = HttpClient(settings, transport=transport)

    attempt = await http.request(
        "POST",
        SUBMIT,
        service="auc",
        json_body={"file_url": "https://x/a.wav"},
    )

    assert attempt.succeeded, attempt.error
    assert json.loads(attempt.response_text)["x_tt_logid"] == "LOGID-PROBE-123"
    assert any(path.endswith("/submit") for path, _ in transport.requests)

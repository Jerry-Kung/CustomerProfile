"""AUC 语音识别：火山引擎云服务的适配层。

**为什么需要这一层，而不是把云调用直接写进工作流定义。**

DSL 的「录音文件内容抽取」把 AUC 调用拆成两个 http 节点（``/submit_analyze`` 提交、
``/query_analyze`` 轮询），下游用两个 code 节点从**响应体**里取 ``task_id`` /
``x_tt_logid`` 与 ``auc_result`` / ``poll_count``。那两个 code 节点是**逐字符取自 DSL** 的，
由 ``tests/test_code_verbatim.py`` 用 AST 比对钉住，不能改。

而火山引擎的云服务是另一套形状：

- ``task_id`` 由**调用方**生成（UUID），提交与查询都要在 header 里带上，服务端不回传；
- 处理状态在**响应头** ``X-Api-Status-Code`` 里，不在响应体；
- 提交后需**由调用方轮询**，直到状态码变成终态；
- 终态响应体是完整的 utterances JSON，而不是内网服务那种「文本化的数组」。

因此这一层的职责是：把云服务的形状**翻译回**那两个 code 节点期待的形状。工作流的图、
两个 HTTP 节点、两个 code 节点都不改——迁移基线因此保持不动（见 ``differences.md`` W12）。

**为什么要保持契约而不是改 code 节点。** 改 code 节点要同时改 DSL 基线、逐字符测试、
以及下游提示词（``auc_result`` 被直接插进校对提示词）。保契约只动传输层一个文件。

**性能上的实质收益。** 内网服务要求调用方自己轮询；这里把轮询收进一次调用内完成，
因此 ``/query_analyze`` 节点返回时结果已经就绪（实测 6 秒音频 4 次轮询、5.5 秒完成）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Mapping

import httpx

from .http import HttpAttempt, _ms

SUBMIT_PATH = "/submit_analyze"
QUERY_PATH = "/query_analyze"
"""对内的两个路径常量。

定义里写的仍是这两个路径（DSL 原值），由这里翻译成云服务的 ``/submit`` 与 ``/query``。
留痕里记的也是这两个值，因此历史运行的 request_attempts 口径不变。
"""

CLOUD_SUBMIT_PATH = "/submit"
CLOUD_QUERY_PATH = "/query"

OK = "20000000"
IN_PROGRESS = frozenset({"20000001", "20000002"})
SILENT_AUDIO = "20000003"
"""静音音频。原 handler 把它当成**成功**并返回一句可解释文案，这里保持同一判定。"""


class AucCloudError(RuntimeError):
    """云服务返回了非预期状态。"""


def _audio_format(file_url: str) -> str:
    """按扩展名推断格式。原实现的规则，保持逐字一致（``.wav`` / ``.mp3``，其余按 wav）。"""
    lowered = file_url.lower()
    if lowered.endswith(".mp3"):
        return "mp3"
    return "wav"


def build_submit_body(file_url: str) -> dict[str, Any]:
    """提交请求体。字段与原 handler 逐项一致，避免行为漂移。"""
    return {
        "user": {"uid": "fake_uid"},
        "audio": {
            "url": file_url,
            "format": _audio_format(file_url),
            "codec": "raw",
            "rate": 48000,
            "bits": 16,
            "channel": 2,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_channel_split": True,
            "enable_ddc": True,
            "enable_speaker_info": True,
            "enable_punc": True,
            "enable_itn": True,
            "ssd_version": "200",
            "model_version": "400",
            "show_speech_rate": True,
            "show_volume": True,
            "enable_emotion_detection": True,
            "enable_gender_detection": True,
            "corpus": {"correct_table_name": "", "context": ""},
        },
    }


def parse_auc_result(input_result: str) -> str:
    """把云服务的 utterances 整理成下游提示词消费的数组文本。

    与原 handler 的 ``parse_auc_result`` 输出**逐字段一致**：``speech_rate`` / ``volume``
    转 float 并保留两位，其余原样。校对提示词的示例正是按这六个字段写的
    （``emotion`` / ``gender`` / ``speaker`` / ``speech_rate`` / ``volume`` / ``text``），
    多一个少一个都会让模型对不齐示例。
    """
    data = json.loads(input_result)
    utterances = data["result"]["utterances"]

    output_list = []
    for item in utterances:
        additions = item.get("additions", {})

        speech_rate = additions.get("speech_rate")
        if speech_rate is not None:
            try:
                speech_rate = round(float(speech_rate), 2)
            except (ValueError, TypeError):
                pass

        volume = additions.get("volume")
        if volume is not None:
            try:
                volume = round(float(volume), 2)
            except (ValueError, TypeError):
                pass

        output_list.append(
            {
                "emotion": additions.get("emotion"),
                "gender": additions.get("gender"),
                "speaker": additions.get("speaker"),
                "speech_rate": speech_rate,
                "volume": volume,
                "text": item.get("text"),
            }
        )

    return json.dumps(output_list, ensure_ascii=False, indent=2)


class AucClient:
    """AUC 云服务客户端。

    对 :class:`~customer_profile.execution.http.HttpClient` 暴露同样的 ``request`` 签名，
    因此节点执行器不需要知道传输换了。差别在于**它在一次调用内跑完提交 + 轮询**：
    ``/submit_analyze`` 只提交，``/query_analyze`` 返回时结果已就绪。

    两者的响应体都被翻译成内网契约，两个 code 节点因此无需改动。
    """

    def __init__(
        self,
        settings: Any,
        *,
        transport: Any = None,
        replay: Any = None,
        request_limiter: Any = None,
        recorder: Any = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._replay = replay
        self._limiter = request_limiter
        self._recorder = recorder
        self._client: httpx.AsyncClient | None = None
        # 提交与查询是两个节点，task_id 必须跨节点传递。云服务不回传它（调用方生成），
        # 所以这里按「同一运行 + 同一提交节点」暂存，供查询节点取用。
        # 键里带 run_id 与 call_path，避免并发运行或迭代内的多次调用互相覆盖。
        self._pending: dict[str, str] = {}

    # ------------------------------------------------------------ 装配

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.auc_base_url,
                timeout=httpx.Timeout(
                    connect=self._settings.http_connect_timeout,
                    read=self._settings.http_read_timeout,
                    write=self._settings.http_write_timeout,
                    pool=self._settings.http_connect_timeout,
                ),
                verify=self._settings.auc_ssl_verify,
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ 对外接口

    async def request(
        self,
        method: str,
        path: str,
        *,
        service: str = "auc",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        ssl_verify: bool | None = None,
        retry_enabled: bool | None = None,
        retry_max: int | None = None,
        node_ref: Any = None,
    ) -> HttpAttempt:
        """与 ``HttpClient.request`` 同签名。``path`` 决定翻译成哪个云调用。"""
        attempt = await self._single_attempt(path, json_body, node_ref)
        await self._record(node_ref, attempt)
        return attempt

    # ------------------------------------------------------------ 内部

    def _headers(self, task_id: str, *, submit: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._settings.auc_api_key,
            "X-Api-Resource-Id": self._settings.auc_resource_id,
            "X-Api-Request-Id": task_id,
        }
        if submit:
            # 提交侧的固定值。查询侧不带它，与原实现一致。
            headers["X-Api-Sequence"] = "-1"
        return headers

    async def _single_attempt(
        self, path: str, json_body: Any, node_ref: Any
    ) -> HttpAttempt:
        attempt = HttpAttempt(
            attempt_no=1,
            method="POST",
            url=path,
            # 凭据经 redact_headers 掩码后再落痕：与 HttpClient 同口径。
            request_headers=_redact(self._headers("", submit=path == SUBMIT_PATH)),
            request_body=json_body,
        )
        started = time.monotonic()

        if self._replay is not None:
            # 回放模式：AUC 这两条路径目前没有 fixture。严格模式下如实报「未命中」，
            # 而不是悄悄出网——回放模式的全部意义就是不出网。
            frame = self._replay.next_http("POST", path)
            attempt.replayed = True
            if frame is None:
                attempt.error = "回放模式：该请求没有匹配的固定响应"
                attempt.error_code = "replay_miss"
                attempt.duration_ms = _ms(started)
                return attempt
            attempt.status_code = int(frame.get("status_code", 200))
            attempt.response_text = frame.get("text", "")
            attempt.response_headers = frame.get("headers", {}) or {}
            attempt.duration_ms = _ms(started)
            return attempt

        try:
            if path == SUBMIT_PATH:
                await self._do_submit(attempt, json_body, node_ref)
            elif path == QUERY_PATH:
                await self._do_query(attempt, json_body, node_ref)
            else:
                raise AucCloudError(
                    f"未知的 AUC 路径 {path!r}；只有 {SUBMIT_PATH} 与 {QUERY_PATH}"
                )
        except AucCloudError as exc:
            attempt.error = str(exc)
            attempt.error_code = "auc_business_error"
        except Exception as exc:  # 传输层异常：与 HttpClient 同口径
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.error_code = "transport_error"

        attempt.duration_ms = _ms(started)
        return attempt

    def _key(self, node_ref: Any) -> str:
        """暂存键。取不到运行上下文时退化成一个常量，保证单测可跑。"""
        run_id = getattr(node_ref, "run_id", "") or ""
        call_path = getattr(node_ref, "call_path", "") or ""
        return f"{run_id}|{call_path}"

    async def _do_submit(
        self, attempt: HttpAttempt, json_body: Any, node_ref: Any
    ) -> None:
        file_url = (json_body or {}).get("file_url") if json_body else None
        if not file_url:
            raise AucCloudError("AUC 提交缺少 file_url")

        task_id = str(uuid.uuid4())
        body = build_submit_body(str(file_url))
        response = await self._post(
            CLOUD_SUBMIT_PATH, body, self._headers(task_id, submit=True)
        )
        attempt.status_code = response.status_code
        attempt.response_headers = _redact(dict(response.headers))
        code = response.headers.get("X-Api-Status-Code", "")
        if code != OK:
            raise AucCloudError(
                f"提交失败：X-Api-Status-Code={code!r} "
                f"X-Api-Message={response.headers.get('X-Api-Message')!r}"
            )

        logid = response.headers.get("X-Tt-Logid", "")
        self._pending[self._key(node_ref)] = task_id
        # 翻译回内网契约：下游 `parse_task_params` 从这里取 task_id / x_tt_logid。
        # task_id 是**我们的** UUID（云服务不回传），x_tt_logid 是服务端的日志号。
        attempt.response_text = json.dumps(
            {"task_id": task_id, "x_tt_logid": logid}, ensure_ascii=False
        )

    async def _do_query(
        self, attempt: HttpAttempt, json_body: Any, node_ref: Any
    ) -> None:
        task_id = self._pending.pop(self._key(node_ref), "")
        if not task_id:
            # 同一运行内提交后才会查询。取不到说明提交没成功，如实报错，
            # 不要把空 task_id 发出去换一个更难懂的 45000000。
            raise AucCloudError(
                "查询时找不到本次运行提交的 task_id（提交节点是否成功？）"
            )

        poll_count = 0
        deadline = self._settings.auc_poll_max_attempts
        while True:
            poll_count += 1
            response = await self._post(
                CLOUD_QUERY_PATH, {}, self._headers(task_id, submit=False)
            )
            code = response.headers.get("X-Api-Status-Code", "")

            if code == OK:
                attempt.status_code = response.status_code
                attempt.response_headers = _redact(dict(response.headers))
                attempt.response_text = json.dumps(
                    {
                        "auc_result": parse_auc_result(response.text),
                        "poll_count": poll_count,
                    },
                    ensure_ascii=False,
                )
                return

            if code == SILENT_AUDIO:
                # 静音音频是**正常业务结论**，不是故障：原 handler 也按成功返回。
                attempt.status_code = response.status_code
                attempt.response_headers = _redact(dict(response.headers))
                attempt.response_text = json.dumps(
                    {
                        "auc_result": json.dumps(
                            [], ensure_ascii=False
                        ),
                        "poll_count": poll_count,
                    },
                    ensure_ascii=False,
                )
                return

            if code not in IN_PROGRESS:
                raise AucCloudError(
                    f"识别失败：X-Api-Status-Code={code!r} "
                    f"X-Api-Message={response.headers.get('X-Api-Message')!r}"
                )

            if poll_count >= deadline:
                raise AucCloudError(
                    f"轮询 {poll_count} 次仍未完成（上限 {deadline} 次，"
                    f"间隔 {self._settings.auc_poll_interval_s}s）"
                )
            await asyncio.sleep(self._settings.auc_poll_interval_s)

    async def _post(
        self, path: str, body: Any, headers: Mapping[str, str]
    ) -> httpx.Response:
        client = await self._http()
        if self._limiter is not None:
            async with self._limiter:
                return await client.post(path, json=body, headers=dict(headers))
        return await client.post(path, json=body, headers=dict(headers))

    async def _record(self, node_ref: Any, attempt: HttpAttempt) -> None:
        if self._recorder is None or node_ref is None:
            return
        await self._recorder(node_ref, attempt)


def _redact(headers: Mapping[str, Any]) -> dict[str, str]:
    """掩码敏感头。与 ``HttpClient`` 共用同一份敏感头清单，避免两处口径分叉。"""
    from .http import redact_headers

    return redact_headers(headers)

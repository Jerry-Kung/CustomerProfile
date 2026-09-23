"""统一 LLM 调用层。

`docs/specs/V0迁移规划.md` §6.4 的实施：**一个** ``llm_call()`` 承载全部 LLM 调用，
替代原 DSL 里 17 处 ``Gemini（异常输出重试版）`` 子流程与全部直连 ``llm`` 节点。

重试下沉到本层（§6.4.3）——只在一层实现，杜绝「SDK 重试 × HTTP 封装重试 × 节点重试 ×
子流程重试」的乘法叠加。因重试不再由图结构自然分开，**每次尝试必须显式留痕**（§6.6）：
attempt 表由本层写入，而不是由调度层猜测。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .executors import NodeExecutionError

# ---------------------------------------------------------------- 异常判定

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINKING_ONLY_MIN_CHARS = 500


@dataclass(frozen=True, slots=True)
class Usage:
    """token 用量。缺失标记为 ``None`` 而不是伪造 0（`Dify迁移任务说明.md` §6）。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def known(self) -> bool:
        return any(
            v is not None
            for v in (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        )

    @property
    def all_zero(self) -> bool:
        values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        return all(v == 0 for v in values)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "Usage":
        if not raw:
            return cls()
        return cls(
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
        )

    def asdict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def detect_output_anomaly(
    text: str | None,
    reasoning_content: str | None = None,
    usage: Usage | None = None,
) -> str | None:
    """判定输出是否中途异常截断，返回原因码；正常则返回 ``None``。

    规则沿用原 DSL「Gemini（异常输出重试版） - 判断结果是否异常」节点代码（§6.4.2）：

    - ``usage_zero``：``usage`` 三项 tokens 全为 0；
    - ``empty_text``：去掉首尾空白后 ``text`` 为空；
    - ``unfinished_think_block``：``text`` 以 ``<think>`` 开头但不含 ``</think>``；
    - ``thinking_only_suspected``：``reasoning_content`` 为空、``text`` 以 ``<think>``
      开头且长度 < 500。

    ``usage`` 缺失（``None`` 或三项皆为 ``None``）不触发 ``usage_zero``——没有依据时
    不能把「未知」当成异常，也不能当成 0。
    """
    usage = usage or Usage()
    if usage.known and usage.all_zero:
        return "usage_zero"

    stripped = (text or "").strip()
    if not stripped:
        return "empty_text"

    if stripped.startswith(_THINK_OPEN) and _THINK_CLOSE not in stripped:
        return "unfinished_think_block"

    if (
        not (reasoning_content or "").strip()
        and stripped.startswith(_THINK_OPEN)
        and len(stripped) < _THINKING_ONLY_MIN_CHARS
    ):
        return "thinking_only_suspected"

    return None


def strip_code_fence(text: str) -> str:
    """去掉 LLM 常见输出的 ```json ... ``` 包裹。

    逻辑取自 DSL 中人工确认提取节点的 ``_strip_code_fence``，保持行为一致。
    """
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json|JSON)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_json_output(text: str) -> Any:
    """解析 JSON 输出，容忍代码块包裹与前后空白。失败抛 :class:`ValueError`。"""
    candidate = strip_code_fence(text)
    if not candidate:
        raise ValueError("JSON 输出为空")
    return json.loads(candidate)


# ---------------------------------------------------------------- 调用

MAX_ATTEMPTS = 3
"""最多 3 次尝试（首次 + 2 次重试）。缺省口径见差异清单 M1，待业务确认。"""


@dataclass(slots=True)
class LlmAttempt:
    """一次 LLM 尝试的留痕数据（对应「请求尝试」层）。"""

    attempt_no: int
    request: dict[str, Any]
    response_text: str | None = None
    reasoning_content: str | None = None
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    error: str | None = None
    error_code: str | None = None
    model: str | None = None
    provider_request_id: str | None = None
    anomaly: str | None = None
    replayed: bool = False
    """是否来自固定响应回放。回放结果不算真实模型效果。"""

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.anomaly is None

    def asdict(self) -> dict[str, Any]:
        return {
            "attempt_no": self.attempt_no,
            "request": self.request,
            "response_text": self.response_text,
            "reasoning_content": self.reasoning_content,
            "usage": self.usage.asdict(),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_code": self.error_code,
            "model": self.model,
            "provider_request_id": self.provider_request_id,
            "anomaly": self.anomaly,
            "replayed": self.replayed,
        }


class LlmCallError(NodeExecutionError):
    """LLM 调用在尝试上限内仍未成功。``attempts`` 保留全部尝试痕迹。"""

    def __init__(self, message: str, attempts: Sequence[LlmAttempt] = ()) -> None:
        super().__init__(message, retryable=False)
        self.attempts = list(attempts)


class LlmClient:
    """统一 LLM 客户端。

    模型、temperature、thinking 全部由 ``Settings`` 给定，不在调用点配置（§6.4.4）：
    全项目同一款模型、``temperature=0.5``、thinking 开启、不设其他参数。
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
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                import httpx

                self._client = httpx.AsyncClient(
                    base_url=self._settings.llm_base_url.rstrip("/"),
                    timeout=httpx.Timeout(
                        connect=self._settings.http_connect_timeout,
                        read=self._settings.http_read_timeout,
                        write=self._settings.http_write_timeout,
                        pool=self._settings.http_connect_timeout,
                    ),
                    headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                    transport=self._transport,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ 组装

    def build_messages(
        self,
        prompt: str,
        system: str | None = None,
        images: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> list[dict[str, Any]]:
        """组装 messages。有图片时用多模态的 content 数组形式。

        ``detail`` 对应 Dify vision 节点的 ``vision.configs.detail``（实测为 ``high``），
        透传给 OpenAI 兼容的 ``image_url.detail``。不设时不下发该字段——缺省行为交给
        模型侧，不替调用方猜。
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})

        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images:
                image_url: dict[str, Any] = {"url": image}
                if detail:
                    image_url["detail"] = detail
                content.append({"type": "image_url", "image_url": image_url})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def build_payload(
        self,
        prompt: str,
        system: str | None = None,
        images: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """构造请求体。按 §6.4.4 只设模型与 temperature，不设其他参数。"""
        return {
            "model": self._settings.llm_model,
            "messages": self.build_messages(prompt, system, images, detail),
            "temperature": self._settings.llm_temperature,
        }

    # ------------------------------------------------------------ 调用

    async def llm_call(
        self,
        prompt: str,
        system: str | None = None,
        images: Sequence[str] | None = None,
        detail: str | None = None,
        expect_json: bool = False,
        node_ref: Any = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> str | dict[str, Any]:
        """调用模型，最多 ``max_attempts`` 次尝试（§6.4.1 + §6.4.2）。

        ``expect_json=True`` 时返回解析后的对象，否则返回文本。判定「是否需要 JSON」
        依据**下游消费方语义**，由调用点显式声明（§6.4.2 的结论）。

        每次尝试都写入 ``node_ref`` 指向的尝试记录，失败时抛 :class:`LlmCallError`
        并把全部尝试带出。
        """
        payload = self.build_payload(prompt, system, images, detail)
        attempts: list[LlmAttempt] = []

        for attempt_no in range(1, max_attempts + 1):
            attempt = await self._single_attempt(payload, attempt_no)
            attempts.append(attempt)
            await self._record_attempt(node_ref, attempt, payload)

            if attempt.succeeded:
                text = attempt.response_text or ""
                if expect_json:
                    try:
                        return parse_json_output(text)
                    except ValueError as exc:
                        # 记下判为异常的原因。尝试本身已在进入循环时写入过记录，
                        # 这里不再重复写一行——否则同一次尝试会占两行，让「尝试次数」
                        # 这类统计失真。
                        attempt.anomaly = f"invalid_json: {exc}"
                        if attempt_no >= max_attempts:
                            break
                        continue
                return text

            if attempt_no >= max_attempts:
                break
            await asyncio.sleep(self._backoff_seconds(attempt_no))

        reasons = "; ".join(
            (a.error or a.anomaly or "unknown") for a in attempts
        )
        raise LlmCallError(
            f"LLM 调用在 {len(attempts)} 次尝试后仍失败（{reasons}）", attempts
        )

    async def _single_attempt(
        self, payload: Mapping[str, Any], attempt_no: int
    ) -> LlmAttempt:
        """一次尝试。异常一律转成 ``LlmAttempt.error``，不向上抛，以便记录全部尝试。"""
        attempt = LlmAttempt(attempt_no=attempt_no, request=dict(payload))
        started = time.monotonic()

        if self._replay is not None:
            attempt.replayed = True
            try:
                frame = self._replay.next_llm(payload)
            except Exception as exc:
                # 严格模式下固定响应未命中。把它转成一次失败的尝试：调用点看到的仍是
                # LlmCallError（重试语义一致），而「未命中」这件事本身也留了痕——
                # 若直接让 ReplayMiss 穿透，调用方既要捕获两种异常，又拿不到尝试记录。
                attempt.error = str(exc)
                attempt.error_code = "replay_miss"
                attempt.duration_ms = _ms(started)
                return attempt
            if frame is None:
                attempt.error = "回放模式：该请求没有匹配的固定响应"
                attempt.error_code = "replay_miss"
                attempt.duration_ms = _ms(started)
                return attempt
            attempt.response_text = frame.get("text")
            attempt.reasoning_content = frame.get("reasoning_content")
            attempt.usage = Usage.from_raw(frame.get("usage"))
            attempt.model = frame.get("model", payload.get("model"))
            attempt.duration_ms = _ms(started)
            attempt.anomaly = detect_output_anomaly(
                attempt.response_text, attempt.reasoning_content, attempt.usage
            )
            return attempt

        try:
            if self._limiter is not None:
                async with self._limiter:
                    response = await self._post(payload)
            else:
                response = await self._post(payload)
        except Exception as exc:  # 网络错误、超时等
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.error_code = "transport_error"
            attempt.duration_ms = _ms(started)
            return attempt

        attempt.duration_ms = _ms(started)
        attempt.provider_request_id = response.headers.get("x-request-id")

        if response.status_code >= 400:
            attempt.error = f"HTTP {response.status_code}: {response.text[:500]}"
            attempt.error_code = f"http_{response.status_code}"
            return attempt

        try:
            body = response.json()
        except ValueError as exc:
            attempt.error = f"响应不是合法 JSON：{exc}"
            attempt.error_code = "bad_response"
            return attempt

        attempt.model = body.get("model")
        attempt.provider_request_id = (
            attempt.provider_request_id or body.get("id")
        )
        attempt.usage = Usage.from_raw(body.get("usage"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        attempt.response_text = message.get("content")
        attempt.reasoning_content = (
            message.get("reasoning_content") or message.get("reasoning")
        )
        attempt.anomaly = detect_output_anomaly(
            attempt.response_text, attempt.reasoning_content, attempt.usage
        )
        return attempt

    async def _post(self, payload: Mapping[str, Any]) -> Any:
        client = await self._ensure_client()
        return await client.post("/chat/completions", json=dict(payload))

    @staticmethod
    def _backoff_seconds(attempt_no: int) -> float:
        return min(2.0, 0.2 * (2 ** (attempt_no - 1)))

    async def _record_attempt(
        self, node_ref: Any, attempt: LlmAttempt, payload: Mapping[str, Any]
    ) -> None:
        if self._recorder is None or node_ref is None:
            return
        await self._recorder(node_ref, attempt, payload)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

"""统一 LLM 调用层。

既定设计原则 的实施：**一个** ``llm_call()`` 承载全部 LLM 调用，
替代迁移前的定义里 17 处 带异常输出重试的 Gemini 子流程与全部直连 ``llm`` 节点。

重试下沉到本层（§6.4.3）——只在一层实现，杜绝「SDK 重试 × HTTP 封装重试 × 节点重试 ×
子流程重试」的乘法叠加。因重试不再由图结构自然分开，**每次尝试必须显式留痕**（§6.6）：
attempt 表由本层写入，而不是由调度层猜测。
"""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .executors import NodeExecutionError
from .. import text_utils as tut

# ---------------------------------------------------------------- 异常判定

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINKING_ONLY_MIN_CHARS = 500


@dataclass(frozen=True, slots=True)
class Usage:
    """token 用量。缺失标记为 ``None`` 而不是伪造 0。"""

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
    finish_reason: str | None = None,
    min_completion_tokens: int | None = None,
) -> str | None:
    """判定输出是否中途异常截断，返回原因码；正常则返回 ``None``。

    规则沿用迁移前的定义「判断结果是否异常」节点的代码（§6.4.2）：

    - ``usage_zero``：``usage`` 三项 tokens 全为 0；
    - ``empty_text``：去掉首尾空白后 ``text`` 为空；
    - ``unfinished_think_block``：``text`` 以 ``<think>`` 开头但不含 ``</think>``；
    - ``thinking_only_suspected``：``reasoning_content`` 为空、``text`` 以 ``<think>``
      开头且长度 < 500。

    v3.3+ 新增（对应跑批可靠性 A3）：

    - ``suspicious_finish_reason``：``finish_reason`` 不是 ``stop`` 且不是 ``None``
      （``length`` / ``content_filter`` / 其它异常终止原因）；
    - ``output_too_short``：指定了 ``min_completion_tokens`` 且实际 ``completion_tokens``
      低于该阈值——用于「输入很大、输出不该很少」的节点。

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

    # v3.3+: 检查 finish_reason
    if finish_reason is not None and finish_reason not in ("stop", "end_turn"):
        # length = 达到 max_tokens 被截断
        # content_filter = 内容过滤触发
        # tool_calls / function_call = 工具调用（本项目不使用）
        # 其它值都是异常终止
        return f"suspicious_finish_reason: {finish_reason}"

    # v3.3+: 输出量下限闸门
    if min_completion_tokens is not None and usage.completion_tokens is not None:
        if usage.completion_tokens < min_completion_tokens:
            return f"output_too_short: {usage.completion_tokens} < {min_completion_tokens}"

    return None


def strip_code_fence(text: str) -> str:
    """去掉 LLM 常见输出的 ```json ... ``` 包裹。

    实现统一在 :func:`customer_profile.text_utils.strip_code_fence`。
    """
    return tut.strip_code_fence(text)


def parse_json_output(text: str) -> Any:
    """解析 JSON 输出，容忍代码块包裹与前后空白。失败抛 :class:`ValueError`。"""
    candidate = strip_code_fence(text)
    if not candidate:
        raise ValueError("JSON 输出为空")
    return json.loads(candidate)


def _bracket_scan(candidate: str) -> tuple[list[str], bool]:
    """走查一遍字符串，返回 ``(未闭合的括号栈, 是否停在未闭合字符串里)``。

    跳过字符串字面量内部的括号（含转义），因此只统计**结构性**括号。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in candidate:
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    return stack, in_string


def close_open_brackets(text: str) -> str:
    """按未闭合的括号栈补齐结尾，得到一个语法完整的候选。

    针对实测的失败形态：模型写完了全部内容，却在**结尾漏掉几个闭合括号**
    （v2.1 的 ``13435844484`` 三次尝试都少两个 ``}``，输出长度与报错位置相等，
    证明不是被截断，是配平缺失）。补的是**结构**，不是内容——内容仍全部来自模型输出。
    """
    candidate = strip_code_fence(text)
    stack, in_string = _bracket_scan(candidate)
    repaired = candidate + ('"' if in_string else "")
    repaired += "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return repaired


def _truncate_to_last_complete(candidate: str) -> str | None:
    """截到最后一个「括号栈回到空」的位置，返回该前缀；没有则 ``None``。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    last_complete: int | None = None
    for index, char in enumerate(candidate):
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                break
            opener = stack.pop()
            if (opener == "{" and char != "}") or (opener == "[" and char != "]"):
                break
            if not stack:
                last_complete = index + 1
    if last_complete is None:
        return None
    return candidate[:last_complete]


def salvage_json_output(text: str) -> tuple[Any | None, str | None]:
    """尽力抢救一个语法不完整的 JSON 输出，返回 ``(对象, 失败原因)``。

    三级策略，逐级放宽：

    1. **补括号**：按未闭合栈补齐结尾（治「内容写完、结尾漏括号」）；
    2. **截断**：截到最后一个完整的顶层对象（治「尾部多写了半句话」）；
    3. **补括号后再截断**：前两者都不成时的兜底。

    抢救成功返回对象、原因为 ``None``；全失败返回 ``None`` 与最后一条错误。
    调用方拿到对象后应**留痕**，让人知道这份产物是抢救出来的、不是完好解析的。
    """
    candidate = strip_code_fence(text)
    if not candidate:
        return None, "JSON 输出为空"

    last_error: str | None = None
    try:
        return json.loads(close_open_brackets(candidate)), None
    except ValueError as exc:
        last_error = f"close_open_brackets: {exc}"

    truncated = _truncate_to_last_complete(candidate)
    if truncated:
        try:
            return json.loads(truncated), None
        except ValueError as exc:
            last_error = f"truncate: {exc}"
        try:
            return json.loads(close_open_brackets(truncated)), None
        except ValueError as exc:
            last_error = f"truncate+close: {exc}"

    return None, last_error or "无法抢救"


def parse_jsonl_output(text: str) -> tuple[list[Any], list[dict[str, Any]]]:
    """解析 JSONL（每行一个 JSON 值），返回 ``(成功解析的列表, 坏行明细)``。

    **坏行只丢那一行，不丢整段**——这正是它比单个大 JSON 更抗崩的地方（v3 任务书 §P1）。
    空行跳过；代码块围栏按行剥除；非 JSON 的说明行记为坏行（留痕不静默）。
    """
    records: list[Any] = []
    dropped: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = strip_code_fence(raw_line)
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            dropped.append({"line": line_no, "error": str(exc), "text": raw_line[:200]})
    return records, dropped


# ---------------------------------------------------------------- 调用

MAX_ATTEMPTS = 3
"""最多 3 次尝试（首次 + 2 次重试）。缺省口径见 ``docs/design/执行语义口径.md`` 的 M1，待业务确认。"""


def structured_response_format(
    model: str, schema: Mapping[str, Any], *, has_images: bool = False
) -> dict[str, Any]:
    """只对声明了对象契约的节点约束输出，不影响 JSONL、数组和普通文本。

    千问 3.8-Flash 系列已确认支持 strict Schema；DeepSeek 等模型使用 JSON Object。
    模型仍从 Settings 读取，这里只维护协议能力；未确认的模型不冒用 Schema。
    """
    model = model.lower().strip()
    if not has_images and (
        model == "qwen3.8-flash" or model.startswith("qwen3.8-flash-")
    ):
        return {
            "type": "json_schema",
            "json_schema": {**deepcopy(schema), "strict": True},
        }
    return {"type": "json_object"}


def _schema_mode_unsupported(attempt: LlmAttempt) -> bool:
    error = (attempt.error or "").lower()
    return (
        attempt.error_code in ("http_400", "http_422")
        and "json_schema" in error
        and any(word in error for word in ("not support", "unsupported", "不支持"))
    )


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
    finish_reason: str | None = None
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
            "finish_reason": self.finish_reason,
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
        quota: Any = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._replay = replay
        self._limiter = request_limiter
        self._recorder = recorder
        self._quota = quota
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

        ``detail`` 对应 ``vision`` 节点的 ``vision.configs.detail``（实测为 ``high``），
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
        """构造请求体。除模型、 messages 与 temperature 外，

        仅在 ``LLM_ENABLE_THINKING`` 被显式设置时额外带上 ``enable_thinking``。
        未设置（``None``）时不发这个键，沿用服务端默认值——
        ``LLM_MAX_TOKENS`` 同理。
        不同服务商对该开关的默认取值不同，发一个我们猜测的值
        会把服务端的默认覆盖掉。
        """
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": self.build_messages(prompt, system, images, detail),
            "temperature": self._settings.llm_temperature,
        }
        if self._settings.llm_enable_thinking is not None:
            payload["enable_thinking"] = self._settings.llm_enable_thinking
        if self._settings.llm_max_tokens is not None:
            payload["max_tokens"] = self._settings.llm_max_tokens
        return payload

    # ------------------------------------------------------------ 调用

    async def llm_call(
        self,
        prompt: str,
        system: str | None = None,
        images: Sequence[str] | None = None,
        detail: str | None = None,
        expect_json: bool = False,
        expect_jsonl: bool = False,
        node_ref: Any = None,
        max_attempts: int = MAX_ATTEMPTS,
        min_completion_tokens: int | None = None,
        response_validator: Callable[[Any], Any] | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str | dict[str, Any]:
        """调用模型，最多 ``max_attempts`` 次尝试（§6.4.1 + §6.4.2）。

        ``expect_json=True`` 时返回解析后的对象，否则返回文本。判定「是否需要 JSON」
        依据**下游消费方语义**，由调用点显式声明（§6.4.2 的结论）。

        ``response_validator`` 在同一尝试预算内校验 JSON 业务形状；失败也留痕并重试。
        启用时不抢救截断 JSON，避免把部分邀约内容当成完整结果。
        格式或业务校验失败时，下一次请求携带上次原始输出及具体错误，重新生成完整对象。
        ``response_schema`` 声明对象契约；按模型能力启用 JSON Object 或 strict JSON Schema。

        ``expect_jsonl=True`` 时按**逐行 JSON** 解析，返回
        ``{"records": [...], "dropped_lines": [...]}``：一行坏了只丢那一行，不丢整段
        （v3 任务书 P1）。它与 ``expect_json`` 互斥，调用点择一。

        **JSON 解析失败先抢救**（v3 P1）：实测失败形态是「内容写完、结尾漏两个括号」，
        不是被截断。因此严格解析失败时先 :func:`salvage_json_output` 补括号/截断；
        抢救得到对象即算成功（否则一次结尾手误就让整案归零）。抢救不到才按原逻辑重试。

        ``min_completion_tokens`` 是**输出量下限**（v3.3 跑批可靠性 A3）：给出时，某次
        尝试的 ``completion_tokens`` 低于它就判为 ``output_too_short`` 异常并按重试语义
        处理。它服务于「输入很大、输出不该很少」的节点——大输入配十几 token 的输出，
        是静默截断的典型形态，而这时 ``finish_reason`` 往往仍是 ``stop``。

        **缺省为 ``None``，即不启用**。这不只是兼容旧行为：短输出不总是故障，
        事簿信息稀薄的案子（本例 ``167****3251`` 只有姓名与称呼两条事实）合法地返回
        ``{"fields": []}``。阈值该由**知道该节点输入规模的调用点**按节点配置给，
        不能在公共层设一个全局值——那会把「数据稀疏」误判成「模型截断」并白烧重试。

        每次尝试都写入 ``node_ref`` 指向的尝试记录，失败时抛 :class:`LlmCallError`
        并把全部尝试带出。
        """
        if response_validator is not None and not expect_json:
            raise ValueError("response_validator 须与 expect_json=True 一起使用")
        if response_schema is not None:
            if response_validator is None or not expect_json or expect_jsonl:
                raise ValueError("response_schema 须与 JSON 对象业务校验一起使用")
            system = (system or "") + "\n请仅输出符合所要求结构的完整 JSON 对象。"
        payload = self.build_payload(prompt, system, images, detail)
        if response_schema is not None:
            payload["response_format"] = structured_response_format(
                self._settings.llm_model, response_schema, has_images=bool(images)
            )
        base_payload = payload
        attempts: list[LlmAttempt] = []

        for attempt_no in range(1, max_attempts + 1):
            attempt = await self._single_attempt(
                payload, attempt_no, min_completion_tokens
            )
            attempts.append(attempt)
            if response_validator is not None:
                # 邀约结果须完整且符合业务契约；截断 JSON 不做补括号抢救。
                # 在落痕前判断，才能保留每次重试的真实原因。
                if attempt.succeeded:
                    try:
                        parsed = parse_json_output(attempt.response_text or "")
                    except ValueError as exc:
                        attempt.anomaly = f"invalid_json: {exc}"
                    else:
                        try:
                            result = response_validator(parsed)
                        except ValueError as exc:
                            attempt.anomaly = f"invalid_output_schema: {exc}"
                await self._record_attempt(node_ref, attempt, payload)
                if attempt.succeeded:
                    return result
                if attempt_no < max_attempts:
                    # 某些代理虽使用相同模型名，却尚未开放 Schema；明确拒绝时在原预算
                    # 内退到 JSON Object。鉴权、限流和一般请求错误不能触发协议降级。
                    if (
                        payload.get("response_format", {}).get("type") == "json_schema"
                        and _schema_mode_unsupported(attempt)
                    ):
                        base_payload = {
                            **base_payload,
                            "response_format": {"type": "json_object"},
                        }
                        payload = base_payload
                    elif (attempt.anomaly or "").startswith(
                        ("invalid_json:", "invalid_output_schema:")
                    ):
                        # 每次基于原始资料和最近一次错误，不累计错误答案；创建新列表，
                        # 避免后续修正污染先前已留痕的请求。
                        payload = {
                            **base_payload,
                            "messages": [
                                *base_payload["messages"],
                                {
                                    "role": "assistant",
                                    "content": (attempt.response_text or "")[:16000],
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "上一次输出未通过校验：\n"
                                        + (attempt.anomaly or "")[:2000]
                                        + "\n请依据原始资料及原定字段结构重新生成完整 JSON 对象，"
                                        "修正字段、类型、层级及业务约束。只返回 JSON，"
                                        "不要解释、Markdown 或局部补丁，不要虚构缺失事实。"
                                    ),
                                },
                            ],
                        }
                    await asyncio.sleep(self._backoff_seconds(attempt_no))
                continue
            await self._record_attempt(node_ref, attempt, payload)

            if attempt.succeeded:
                text = attempt.response_text or ""
                if expect_jsonl:
                    records, dropped = parse_jsonl_output(text)
                    if records or not dropped:
                        return {"records": records, "dropped_lines": dropped}
                    attempt.anomaly = f"invalid_jsonl: {len(dropped)} 行无法解析"
                    if attempt_no >= max_attempts:
                        break
                    continue
                if expect_json:
                    try:
                        return parse_json_output(text)
                    except ValueError as exc:
                        salvaged, _reason = salvage_json_output(text)
                        if salvaged is not None:
                            return salvaged
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
        self,
        payload: Mapping[str, Any],
        attempt_no: int,
        min_completion_tokens: int | None = None,
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
            attempt.finish_reason = frame.get("finish_reason")
            attempt.anomaly = detect_output_anomaly(
                attempt.response_text,
                attempt.reasoning_content,
                attempt.usage,
                attempt.finish_reason,
                min_completion_tokens,
            )
            return attempt

        # 速率额度在**并发名额之前**申请：速率等待可能长达数十秒，若先占住并发名额
        # 等待，会把其它本可以出网的请求一起堵住。
        #
        # 接线点必须在**每次真实出网**这一层（即 _single_attempt），不能放在 llm_call
        # 的循环外层——那样 3 次尝试只会记 1 个请求，配额被系统性低估 3 倍。
        #
        # 回放模式已在上面提前返回，因此不会走到这里：回放不是真实请求，不该消耗额度。
        reserved = 0.0
        if self._quota is not None:
            reserved = await self._quota.acquire(payload)

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
        # 按真实用量校正 TPM 预扣。放在解析出 usage 之后、返回之前：校正本身是记账，
        # 不该影响这次尝试的成败判定。失败路径（非 2xx、坏响应）不校正——「未知」
        # 不能当成 0 退还，那会凭空放大可用额度。
        if self._quota is not None and reserved:
            await self._quota.settle(reserved, attempt.usage)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        attempt.response_text = message.get("content")
        attempt.reasoning_content = (
            message.get("reasoning_content") or message.get("reasoning")
        )
        attempt.finish_reason = choice.get("finish_reason")
        attempt.anomaly = detect_output_anomaly(
            attempt.response_text,
            attempt.reasoning_content,
            attempt.usage,
            attempt.finish_reason,
            min_completion_tokens,
        )
        return attempt

    async def _post(self, payload: Mapping[str, Any]) -> Any:
        client = await self._ensure_client()
        return await client.post(
            self._settings.llm_chat_completions_path, json=dict(payload)
        )

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

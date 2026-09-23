"""外部 HTTP 客户端。

三件事必须在这里做对：

1. **不把 DSL 原值直接传给 HTTP 库。** ``timeout: 0``、无单位的 ``retry_interval``
   语义不明（规划 Q5），一律走显式配置的默认值。
2. **写请求不自动重试。** 超时可能发生在服务端已完成写入之后（`Dify迁移任务说明.md`
   §7.1）。POST 默认 ``retry_max=0``，需要重试须在节点配置里显式声明。
3. **凭据永不落痕。** ``X-API-Key``、``Authorization`` 在写记录前被替换为掩码（§6）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from .context import coerce_to_text

SENSITIVE_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "x-auth-token", "cookie", "set-cookie"}
)
MASK = "***REDACTED***"

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """掩码敏感请求头。任何写入记录或日志的 headers 都必须先过这里。"""
    if not headers:
        return {}
    return {
        key: (MASK if key.lower() in SENSITIVE_HEADERS else coerce_to_text(value))
        for key, value in headers.items()
    }


def redact_url(url: str, secrets: Sequence[str] = ()) -> str:
    """掩码出现在 URL 中的凭据值。"""
    result = url
    for secret in secrets:
        if secret:
            result = result.replace(secret, MASK)
    return result


@dataclass(slots=True)
class HttpAttempt:
    """一次 HTTP 尝试的留痕数据。"""

    attempt_no: int
    method: str
    url: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_text: str | None = None
    duration_ms: int = 0
    error: str | None = None
    error_code: str | None = None
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.status_code is not None and self.status_code < 400

    def asdict(self) -> dict[str, Any]:
        return {
            "attempt_no": self.attempt_no,
            "method": self.method,
            "url": self.url,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "status_code": self.status_code,
            "response_headers": self.response_headers,
            "response_text": self.response_text,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_code": self.error_code,
            "replayed": self.replayed,
        }


class HttpCallError(Exception):
    """HTTP 调用在尝试上限内仍未成功。``attempts`` 保留全部尝试痕迹。"""

    def __init__(self, message: str, attempts: Sequence[HttpAttempt] = ()) -> None:
        super().__init__(message)
        self.attempts = list(attempts)


class HttpClient:
    """按服务名分派的外部 HTTP 客户端。"""

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
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 客户端

    async def _client_for(self, service: str, ssl_verify: bool | None = None) -> Any:
        key = f"{service}:{ssl_verify}"
        if key in self._clients:
            return self._clients[key]
        async with self._lock:
            if key not in self._clients:
                verify = (
                    self._settings.auc_ssl_verify
                    if ssl_verify is None
                    else ssl_verify
                )
                self._clients[key] = httpx.AsyncClient(
                    base_url=self._settings.base_url_for(service),
                    timeout=httpx.Timeout(
                        connect=self._settings.http_connect_timeout,
                        read=self._settings.http_read_timeout,
                        write=self._settings.http_write_timeout,
                        pool=self._settings.http_connect_timeout,
                    ),
                    verify=verify,
                    transport=self._transport,
                )
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------ 调用

    async def request(
        self,
        method: str,
        path: str,
        *,
        service: str = "profile",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        ssl_verify: bool | None = None,
        retry_enabled: bool | None = None,
        retry_max: int | None = None,
        node_ref: Any = None,
    ) -> HttpAttempt:
        """发起一次请求，返回**最后一次成功的尝试**；全部失败时抛 :class:`HttpCallError`。

        重试策略：GET 类幂等方法按配置重试；写方法默认不重试，除非显式传
        ``retry_enabled=True``（调用方需自行确认接口幂等，规划 Q2）。
        """
        method = method.upper()
        attempts: list[HttpAttempt] = []
        max_attempts = self._resolve_attempts(method, retry_enabled, retry_max)

        for attempt_no in range(1, max_attempts + 1):
            attempt = await self._single_attempt(
                method, path, service, headers, params, json_body, content, ssl_verify,
                attempt_no,
            )
            attempts.append(attempt)
            await self._record(node_ref, attempt)

            if attempt.succeeded:
                return attempt
            if attempt_no >= max_attempts:
                break
            await asyncio.sleep(self._settings.http_retry_interval_ms / 1000.0)

        reasons = "; ".join((a.error or f"HTTP {a.status_code}") for a in attempts)
        raise HttpCallError(
            f"{method} {path} 在 {len(attempts)} 次尝试后仍失败（{reasons}）", attempts
        )

    def _resolve_attempts(
        self, method: str, retry_enabled: bool | None, retry_max: int | None
    ) -> int:
        if not self._settings.http_retry_enabled:
            return 1
        if method in IDEMPOTENT_METHODS:
            enabled = True if retry_enabled is None else retry_enabled
            budget = self._settings.http_retry_max if retry_max is None else retry_max
        else:
            # 写方法：默认不自动重试（Q2 缺省处理）
            enabled = bool(retry_enabled)
            budget = 0 if retry_max is None else retry_max
        return max(1, budget + 1) if enabled else 1

    async def _single_attempt(
        self,
        method: str,
        path: str,
        service: str,
        headers: Mapping[str, str] | None,
        params: Mapping[str, Any] | None,
        json_body: Any,
        content: bytes | None,
        ssl_verify: bool | None,
        attempt_no: int,
    ) -> HttpAttempt:
        secrets = [
            self._settings.api_key_for(service),
            self._settings.llm_api_key,
        ]
        # 留痕与回放都按**服务相对路径**寻址，不拼 base_url。
        # 理由：端点随环境变化（.env），而固定响应 fixture 应当与端点无关，
        # 换测试环境不必重录。绝对 URL 只用于真正发请求。
        attempt = HttpAttempt(
            attempt_no=attempt_no,
            method=method,
            url=path,
            request_headers=redact_headers(headers),
            request_body=json_body,
        )
        started = time.monotonic()

        if self._replay is not None:
            frame = self._replay.next_http(method, path)
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
            client = await self._client_for(service, ssl_verify)
            if self._limiter is not None:
                async with self._limiter:
                    response = await client.request(
                        method,
                        path,
                        headers=dict(headers) if headers else None,
                        params=dict(params) if params else None,
                        json=json_body,
                        content=content,
                    )
            else:
                response = await client.request(
                    method,
                    path,
                    headers=dict(headers) if headers else None,
                    params=dict(params) if params else None,
                    json=json_body,
                    content=content,
                )
        except Exception as exc:
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.error_code = "transport_error"
            attempt.duration_ms = _ms(started)
            return attempt

        attempt.duration_ms = _ms(started)
        attempt.status_code = response.status_code
        attempt.response_headers = redact_headers(dict(response.headers))
        attempt.response_text = response.text
        if response.status_code >= 400:
            attempt.error = f"HTTP {response.status_code}"
            attempt.error_code = f"http_{response.status_code}"
        return attempt

    async def _record(self, node_ref: Any, attempt: HttpAttempt) -> None:
        if self._recorder is None or node_ref is None:
            return
        await self._recorder(node_ref, attempt)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

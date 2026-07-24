from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from src.config.settings import QingTianConfig
from src.config.observability import elapsed_ms, mask_phone, safe_text
from src.infrastructure.qingtian.errors import (
    QingTianOutputInvalidJson,
    QingTianRequestFailed,
    QingTianTimeout,
    QingTianWorkflowTerminated,
)

_log = logging.getLogger(__name__)

RUN_PATH = "/run_app_workflow"
QUERY_PATH = "/query_run_app_process"

_NON_TERMINAL_STATUSES = {"to_start", "processing"}
_SUCCESS_STATUS = "success"
_FAILURE_STATUSES = {"failed", "stopped", "interrupted", "timeout"}


def _parse_retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        # HTTP-date values are intentionally ignored; the worker's configured
        # bounded backoff remains safe when the proxy does not send delta-seconds.
        return None
    return seconds if seconds >= 0 else None


@dataclass
class QingTianClient:
    config: QingTianConfig
    _unknown_status_seen: set[str] = field(default_factory=set, init=False, repr=False)

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        """Legacy convenience path: submit and poll in one process.

        Retry-enabled workers use :meth:`submit_workflow` and
        :meth:`poll_workflow` separately so the returned runId can be written to
        MySQL before polling starts.  Keeping this method preserves the existing
        platform-neutral ``WorkflowClient`` contract for non-retry deployments.
        """
        workflow_start = time.perf_counter()
        api_key = self._pick_api_key(task_type=task_type, work_env=work_env)
        headers = {
            "Apikey": api_key,
            "Content-Type": "application/json",
        }
        input_data = self._build_inputs(
            customer_phonenumber=customer_phonenumber,
            work_env=work_env,
            batch_id=batch_id,
        )
        run_body: dict[str, Any] = {
            "InputData": json.dumps(input_data, ensure_ascii=False),
            "UserID": self.config.user_id,
            "AppKey": api_key,
        }

        poll_timeout_sec = max(10.0, self.config.poll_timeout_ms / 1000)
        timeout_submit = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
        timeout_poll = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)

        run_url = f"{self.config.base_url.rstrip('/')}{RUN_PATH}"
        query_url = f"{self.config.base_url.rstrip('/')}{QUERY_PATH}"
        run_parts = urlparse(run_url)
        _log.info(
            "qingtian_run_workflow_started customer_phone_masked=%s endpoint_host=%s endpoint_path=%s input_keys=%s",
            mask_phone(customer_phonenumber),
            run_parts.netloc,
            run_parts.path,
            sorted(input_data.keys()),
        )

        with httpx.Client(timeout=None) as client:
            submit_start = time.perf_counter()
            run_id = self._submit_with_client(
                client=client,
                url=run_url,
                body=run_body,
                headers=headers,
                timeout=timeout_submit,
                started_at=workflow_start,
            )
            _log.info(
                "qingtian_workflow_submitted 说明=擎天任务提交成功，开始轮询同一个运行ID run_id=%s submit_elapsed_ms=%s",
                run_id,
                elapsed_ms(submit_start),
            )
            output = self._poll_with_client(
                client=client,
                url=query_url,
                run_id=run_id,
                api_key=api_key,
                headers=headers,
                poll_timeout_sec=poll_timeout_sec,
                timeout=timeout_poll,
                started_at=workflow_start,
            )
            _log.info(
                "qingtian_run_workflow_completed run_id=%s status=success total_elapsed_ms=%s output_size_bytes=%s",
                run_id,
                elapsed_ms(workflow_start),
                len(output.encode("utf-8")),
            )
            return output

    def submit_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        """Start a QingTian workflow and return its runId without polling it."""
        started_at = time.perf_counter()
        api_key = self._pick_api_key(task_type=task_type, work_env=work_env)
        input_data = self._build_inputs(
            customer_phonenumber=customer_phonenumber,
            work_env=work_env,
            batch_id=batch_id,
        )
        run_url = f"{self.config.base_url.rstrip('/')}{RUN_PATH}"
        run_parts = urlparse(run_url)
        _log.info(
            "qingtian_submit_started customer_phone_masked=%s endpoint_host=%s endpoint_path=%s input_keys=%s",
            mask_phone(customer_phonenumber),
            run_parts.netloc,
            run_parts.path,
            sorted(input_data.keys()),
        )
        headers = {"Apikey": api_key, "Content-Type": "application/json"}
        body: dict[str, Any] = {
            "InputData": json.dumps(input_data, ensure_ascii=False),
            "UserID": self.config.user_id,
            "AppKey": api_key,
        }
        timeout = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
        with httpx.Client(timeout=None) as client:
            return self._submit_with_client(
                client=client,
                url=run_url,
                body=body,
                headers=headers,
                timeout=timeout,
                started_at=started_at,
            )

    def poll_workflow(
        self,
        *,
        workflow_run_id: str,
        work_env: str = "test",
        task_type: str = "customer_profile",
        on_poll: Callable[[], None] | None = None,
    ) -> str:
        """Poll a previously acknowledged QingTian run without resubmitting it."""
        started_at = time.perf_counter()
        api_key = self._pick_api_key(task_type=task_type, work_env=work_env)
        headers = {"Apikey": api_key, "Content-Type": "application/json"}
        query_url = f"{self.config.base_url.rstrip('/')}{QUERY_PATH}"
        timeout = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)
        with httpx.Client(timeout=None) as client:
            return self._poll_with_client(
                client=client,
                url=query_url,
                run_id=workflow_run_id,
                api_key=api_key,
                headers=headers,
                poll_timeout_sec=max(10.0, self.config.poll_timeout_ms / 1000),
                timeout=timeout,
                started_at=started_at,
                on_poll=on_poll,
            )

    def _submit_with_client(
        self,
        *,
        client: httpx.Client,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
        started_at: float,
    ) -> str:
        try:
            run_id = self._submit_workflow(
                client=client,
                url=url,
                body=body,
                headers=headers,
                timeout=timeout,
            )
            if not run_id:
                raise QingTianRequestFailed(
                    "runId missing from /run_app_workflow response",
                    stage="submit",
                )
            return run_id
        except (QingTianRequestFailed, QingTianTimeout):
            raise
        except httpx.HTTPError as exc:
            self._raise_http_error(exc, stage="submit", started_at=started_at)

    def _poll_with_client(
        self,
        *,
        client: httpx.Client,
        url: str,
        run_id: str,
        api_key: str,
        headers: dict[str, str],
        poll_timeout_sec: float,
        timeout: httpx.Timeout,
        started_at: float,
        on_poll: Callable[[], None] | None = None,
    ) -> str:
        try:
            return self._poll_run_result(
                client=client,
                url=url,
                run_id=run_id,
                api_key=api_key,
                headers=headers,
                poll_timeout_sec=poll_timeout_sec,
                timeout=timeout,
                on_poll=on_poll,
            )
        except (QingTianRequestFailed, QingTianTimeout, QingTianOutputInvalidJson):
            raise
        except httpx.HTTPError as exc:
            self._raise_http_error(exc, stage="poll", started_at=started_at)

    def _raise_http_error(self, exc: httpx.HTTPError, *, stage: str, started_at: float) -> None:
        if isinstance(exc, httpx.TimeoutException):
            _log.warning(
                "qingtian_http_timeout phase=%s error_type=%s total_elapsed_ms=%s",
                stage,
                type(exc).__name__,
                elapsed_ms(started_at),
            )
            raise QingTianTimeout(f"qingtian {stage} request timed out", stage=stage) from exc
        if isinstance(exc, httpx.HTTPStatusError):
            body_preview = ""
            try:
                body_preview = (exc.response.text or "")[:800]
            except Exception:
                pass
            status_code = exc.response.status_code
            retry_after = _parse_retry_after_seconds(exc.response.headers.get("Retry-After"))
            _log.warning(
                "qingtian_http_error phase=%s status=%s endpoint_host=%s endpoint_path=%s response_request_id=%s detail=%s total_elapsed_ms=%s",
                stage,
                status_code,
                exc.request.url.host,
                exc.request.url.path,
                exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id"),
                safe_text(body_preview or exc, 800),
                elapsed_ms(started_at),
            )
            # Only an explicit 429 is treated as safe to resubmit without a
            # remote id.  A 5xx might have created a run before failing to
            # return its response, so retry workers treat it as unknown.
            raise QingTianRequestFailed(
                f"qingtian HTTP {status_code} {exc.request.url}: {body_preview or str(exc)}",
                stage=stage,
                status_code=status_code,
                retry_after_seconds=retry_after,
                retryable=(stage == "submit" and status_code == 429),
            ) from exc
        _log.warning(
            "qingtian_transport_error phase=%s error_type=%s detail=%s total_elapsed_ms=%s",
            stage,
            type(exc).__name__,
            safe_text(exc, 800),
            elapsed_ms(started_at),
        )
        raise QingTianRequestFailed(
            f"qingtian {stage} request failed: {exc}",
            stage=stage,
            # A connect failure happens before a usable HTTP response exists;
            # allow a bounded retry.  Other transport failures are unknown.
            retryable=(stage == "submit" and isinstance(exc, httpx.ConnectError)),
        ) from exc

    def _submit_workflow(
        self,
        *,
        client: httpx.Client,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> str | None:
        response = client.post(url, json=body, headers=headers, timeout=timeout)
        response.raise_for_status()
        response_headers = getattr(response, "headers", {}) or {}
        response_content = getattr(response, "content", b"") or b""
        _log.debug(
            "qingtian_submit_http_completed status=%s response_request_id=%s response_size_bytes=%s",
            getattr(response, "status_code", None),
            response_headers.get("x-request-id") or response_headers.get("request-id"),
            len(response_content),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise QingTianRequestFailed(
                "qingtian /run_app_workflow response is not valid JSON",
                stage="submit",
            ) from exc
        if not isinstance(payload, dict):
            raise QingTianRequestFailed(
                "qingtian /run_app_workflow response is not a JSON object",
                stage="submit",
            )
        run_id = payload.get("runId") or payload.get("RunID") or payload.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id
        return None

    def _poll_run_result(
        self,
        *,
        client: httpx.Client,
        url: str,
        run_id: str,
        api_key: str,
        headers: dict[str, str],
        poll_timeout_sec: float,
        timeout: httpx.Timeout,
        on_poll: Callable[[], None] | None = None,
    ) -> str:
        interval_sec = max(0.2, self.config.poll_interval_ms / 1000)
        deadline = time.monotonic() + max(1.0, poll_timeout_sec)
        query_body: dict[str, Any] = {
            "RunID": run_id,
            "UserID": self.config.user_id,
            "AppKey": api_key,
        }
        poll_start = time.perf_counter()
        poll_count = 0
        previous_status: str | None = None
        while True:
            if on_poll is not None:
                on_poll()
            poll_count += 1
            response = client.post(url, json=query_body, headers=headers, timeout=timeout)
            response.raise_for_status()
            try:
                body = response.json()
            except ValueError as exc:
                raise QingTianRequestFailed(
                    "qingtian poll response is not valid JSON",
                    stage="poll",
                ) from exc
            if not isinstance(body, dict):
                raise QingTianRequestFailed(
                    "qingtian poll response is not a JSON object",
                    stage="poll",
                )

            status = self._extract_status(body)
            if status != previous_status:
                _log.info(
                    "qingtian_poll_status_changed 说明=擎天任务状态发生变化 run_id=%s previous_status=%s status=%s poll_count=%s elapsed_ms=%s response_summary=%s",
                    run_id,
                    previous_status,
                    status,
                    poll_count,
                    elapsed_ms(poll_start),
                    json.dumps(self._response_summary(body), ensure_ascii=False, default=str),
                )
                previous_status = status
            if status == _SUCCESS_STATUS:
                self._log_terminal_response(run_id=run_id, status=status, body=body)
                output = body.get("output")
                if not isinstance(output, str) or not output.strip():
                    raise QingTianOutputInvalidJson(
                        "qingtian success response missing output string"
                    )
                _log.info(
                    "qingtian_poll_completed run_id=%s status=%s poll_count=%s elapsed_ms=%s",
                    run_id,
                    status,
                    poll_count,
                    elapsed_ms(poll_start),
                )
                return output

            if status in _FAILURE_STATUSES:
                self._log_terminal_response(run_id=run_id, status=status, body=body)
                message = body.get("message") or body.get("msg") or ""
                code = body.get("code")
                raise QingTianWorkflowTerminated(
                    f"qingtian workflow terminated status={status} code={code} message={message}",
                    terminal_status=status,
                )

            if status not in _NON_TERMINAL_STATUSES:
                if status not in self._unknown_status_seen:
                    self._unknown_status_seen.add(status)
                    _log.warning(
                        "qingtian_unknown_status 说明=后端无法识别该擎天状态，将继续轮询直到成功、失败或超时 run_id=%s status=%s response_summary=%s",
                        run_id,
                        status,
                        json.dumps(self._response_summary(body), ensure_ascii=False, default=str),
                    )

            if time.monotonic() >= deadline:
                _log.warning(
                    "qingtian_poll_timeout 说明=轮询擎天超过最大等待时间，任务将按本地重试策略处理 run_id=%s last_status=%s poll_count=%s elapsed_ms=%s last_response_summary=%s",
                    run_id,
                    status,
                    poll_count,
                    elapsed_ms(poll_start),
                    json.dumps(self._response_summary(body), ensure_ascii=False, default=str),
                )
                raise QingTianTimeout("polling qingtian workflow result timed out", stage="poll")
            time.sleep(interval_sec)

    @staticmethod
    def _extract_status(body: dict[str, Any]) -> str:
        value = body.get("status")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _response_summary(body: dict[str, Any]) -> dict[str, Any]:
        """Describe response shape without printing profile contents or credentials."""
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        output = body.get("output")
        summary: dict[str, Any] = {
            "body_keys": sorted(body.keys()),
            "status": body.get("status"),
            "nested_status": data.get("status") if data else None,
            "code": body.get("code"),
            "message": safe_text(body.get("message") or body.get("msg") or "", 200),
            "data_keys": sorted(data.keys()) if data else None,
            "output_type": type(output).__name__,
            "output_size_bytes": len(output.encode("utf-8")) if isinstance(output, str) else None,
        }
        if isinstance(output, str) and output.strip():
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError as exc:
                summary["output_json_valid"] = False
                summary["output_json_error_position"] = exc.pos
            else:
                summary["output_json_valid"] = True
                summary["output_json_type"] = type(parsed).__name__
                if isinstance(parsed, dict):
                    customer = parsed.get("customer") if isinstance(parsed.get("customer"), dict) else None
                    summary.update(
                        {
                            "output_keys": sorted(parsed.keys()),
                            "has_card_id": bool(parsed.get("card_id")),
                            "has_nickname": bool(parsed.get("nickname")),
                            "has_customer_nickname": bool(customer and customer.get("nickname")),
                        }
                    )
        return summary

    @staticmethod
    def _log_terminal_response(*, run_id: str, status: str, body: dict[str, Any]) -> None:
        """Structural diagnostics on terminal QingTian responses (mirrors DifyClient._log_terminal_response)."""
        try:
            nodes = body.get("nodes") if isinstance(body.get("nodes"), dict) else None
            steps = body.get("steps") if isinstance(body.get("steps"), list) else None
            body_json = json.dumps(body, ensure_ascii=False, default=str)
            _log.info(
                "qingtian_poll_terminal_response 说明=收到擎天终态响应 run_id=%s status=%s "
                "body_keys=%s node_count=%s steps_count=%s cost_ms=%s "
                "has_output=%s body_size_bytes=%s response_summary=%s",
                run_id,
                status,
                list(body.keys()),
                len(nodes) if nodes is not None else None,
                len(steps) if steps is not None else None,
                body.get("costMs"),
                isinstance(body.get("output"), str),
                len(body_json),
                json.dumps(QingTianClient._response_summary(body), ensure_ascii=False, default=str),
            )
        except Exception:
            _log.exception("qingtian_poll_terminal_response_log_failed runId=%s", run_id)

    def _pick_api_key(self, *, task_type: str, work_env: str) -> str:
        task = (task_type or "customer_profile").strip().lower()
        if task == "invitation_strategy":
            return self.config.invitation_strategy_api_key
        env = (work_env or "test").strip().lower()
        if env == "production":
            return self.config.api_key_production
        return self.config.api_key_test

    @staticmethod
    def _build_inputs(
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "phone_number": customer_phonenumber,
            "work_env": (work_env or "test").strip().lower(),
            "batch_id": batch_id if batch_id is not None else "",
        }

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.config.settings import DifyConfig
from src.infrastructure.dify.errors import DifyOutputInvalidJson, DifyRequestFailed, DifyTimeout

_log = logging.getLogger(__name__)


@dataclass
class DifyClient:
    config: DifyConfig

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        payload = {
            "inputs": self._build_inputs(
                customer_phonenumber=customer_phonenumber,
                work_env=work_env,
                batch_id=batch_id,
            ),
            "response_mode": "streaming",
            "user": self.config.user_id,
        }
        headers = {
            "Authorization": f"Bearer {self._pick_api_key(task_type=task_type, work_env=work_env)}",
            "Content-Type": "application/json",
        }
        _log.info(
            "dify_run_workflow_start image_mode=%s task_type=%s work_env=%s",
            self.config.image_mode,
            task_type,
            work_env,
        )
        stream_init_timeout_sec = max(5.0, self.config.stream_init_timeout_ms / 1000)
        poll_timeout_sec = max(10.0, self.config.poll_timeout_ms / 1000)
        # Per-request timeouts: Dify "blocking" POST does not return until the workflow finishes, so its
        # read timeout must cover long runs — do NOT reuse the short streaming first-byte timeout.
        timeout_stream = httpx.Timeout(connect=20.0, read=stream_init_timeout_sec, write=120.0, pool=20.0)
        timeout_blocking_start = httpx.Timeout(connect=20.0, read=poll_timeout_sec, write=120.0, pool=20.0)
        timeout_poll = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)
        upload_read_write = max(120.0, min(600.0, poll_timeout_sec))
        timeout_upload = httpx.Timeout(
            connect=20.0, read=upload_read_write, write=upload_read_write, pool=20.0
        )
        try:
            # Disable client default timeout; every call below passes an explicit Timeout.
            with httpx.Client(timeout=None) as client:
                inputs = self._resolve_image_inputs(
                    client=client,
                    headers=headers,
                    raw_inputs=payload["inputs"],
                    timeout=timeout_upload,
                )
                payload["inputs"] = inputs
                _log.info(
                    "dify_inputs_ready image_mode=%s input_keys=%s",
                    self.config.image_mode,
                    sorted(inputs.keys()),
                )
                _log.info(
                    "dify_full_request_payload: %s",
                    json.dumps(payload, ensure_ascii=False, indent=2)
                )
                workflow_run_id = self._start_workflow_and_get_run_id(
                    client=client,
                    payload=payload,
                    headers=headers,
                    stream_init_timeout_sec=stream_init_timeout_sec,
                    timeout_stream=timeout_stream,
                    timeout_blocking_start=timeout_blocking_start,
                )
                if not workflow_run_id:
                    raise DifyRequestFailed("workflow_run_id not found in streaming response")
                response_body = self._poll_run_result(
                    client=client,
                    workflow_run_id=workflow_run_id,
                    headers=headers,
                    poll_timeout_sec=poll_timeout_sec,
                    timeout_poll=timeout_poll,
                )
                return extract_output_string(response_body)
        except httpx.TimeoutException as exc:
            raise DifyTimeout("dify request timed out") from exc
        except httpx.HTTPStatusError as exc:
            body_preview = ""
            try:
                body_preview = (exc.response.text or "")[:800]
            except Exception:
                pass
            raise DifyRequestFailed(
                f"dify HTTP {exc.response.status_code} {exc.request.url}: {body_preview or str(exc)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DifyRequestFailed(f"dify request failed: {exc}") from exc

    def _start_workflow_and_get_run_id(
        self,
        *,
        client: httpx.Client,
        payload: dict[str, Any],
        headers: dict[str, str],
        stream_init_timeout_sec: float,
        timeout_stream: httpx.Timeout,
        timeout_blocking_start: httpx.Timeout,
    ) -> str | None:
        run_parts = urlparse(self.config.run_url)
        try:
            _log.info(
                "dify_workflow_stream_post netloc=%s path=%s",
                run_parts.netloc or "",
                run_parts.path or "",
            )
            with client.stream(
                "POST",
                self.config.run_url,
                json=payload,
                headers=headers,
                timeout=timeout_stream,
            ) as response:
                response.raise_for_status()
                workflow_run_id = self._extract_workflow_run_id(response, timeout_sec=stream_init_timeout_sec)
                if workflow_run_id:
                    return workflow_run_id
        except httpx.ReadTimeout:
            # Some Dify deployments may not flush early SSE events.
            pass

        # Non-SSE POST with response_mode=streaming is unreliable; use blocking to obtain workflow_run_id JSON.
        blocking_payload = dict(payload)
        blocking_payload["response_mode"] = "blocking"
        workflow_run_id = self._start_streaming_without_sse(
            client=client, payload=blocking_payload, headers=headers, timeout=timeout_blocking_start
        )
        return workflow_run_id

    def _poll_run_result(
        self,
        *,
        client: httpx.Client,
        workflow_run_id: str,
        headers: dict[str, str],
        poll_timeout_sec: float,
        timeout_poll: httpx.Timeout,
    ) -> dict[str, Any]:
        poll_url = f"{self.config.base_url.rstrip('/')}/v1/workflows/run/{workflow_run_id}"
        interval_sec = max(0.2, self.config.poll_interval_ms / 1000)
        deadline = time.monotonic() + max(1.0, poll_timeout_sec)
        while True:
            response = client.get(poll_url, headers=headers, timeout=timeout_poll)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise DifyRequestFailed("poll response is not a JSON object")
            status = self._extract_status(body)
            if status in {"succeeded", "failed", "stopped"}:
                self._log_terminal_response(
                    workflow_run_id=workflow_run_id, status=status, body=body
                )
                return body
            if time.monotonic() >= deadline:
                raise DifyTimeout("polling workflow result timed out")
            time.sleep(interval_sec)

    @staticmethod
    def _log_terminal_response(
        *, workflow_run_id: str, status: str, body: dict[str, Any]
    ) -> None:
        """Record envelope shape of every Dify terminal response — feeds extraction-failure triage.

        Body preview is DEBUG-only because it may contain customer data; INFO level emits only structural metadata.
        """
        try:
            data = body.get("data") if isinstance(body.get("data"), dict) else None
            outputs = body.get("outputs") if isinstance(body.get("outputs"), dict) else None
            data_outputs = (
                data.get("outputs")
                if isinstance(data, dict) and isinstance(data.get("outputs"), dict)
                else None
            )
            body_json = json.dumps(body, ensure_ascii=False, default=str)
            _log.info(
                "dify_poll_terminal_response workflow_run_id=%s status=%s "
                "body_keys=%s data_keys=%s outputs_keys=%s data_outputs_keys=%s "
                "has_top_output=%s has_data_output=%s has_outputs_output=%s has_data_outputs_output=%s "
                "body_size_bytes=%s",
                workflow_run_id,
                status,
                list(body.keys()),
                list(data.keys()) if data is not None else None,
                list(outputs.keys()) if outputs is not None else None,
                list(data_outputs.keys()) if data_outputs is not None else None,
                isinstance(body.get("output"), str),
                isinstance(data.get("output"), str) if data is not None else False,
                isinstance(outputs.get("output"), str) if outputs is not None else False,
                isinstance(data_outputs.get("output"), str) if data_outputs is not None else False,
                len(body_json),
            )
            _log.debug(
                "dify_poll_terminal_response_body workflow_run_id=%s body_preview=%s",
                workflow_run_id,
                body_json[:2000],
            )
        except Exception:
            _log.exception(
                "dify_poll_terminal_response_log_failed workflow_run_id=%s", workflow_run_id
            )

    @staticmethod
    def _extract_status(body: dict[str, Any]) -> str:
        if isinstance(body.get("status"), str):
            return body["status"]
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            return data["status"]
        return ""

    def _pick_api_key(self, *, task_type: str, work_env: str) -> str:
        task = (task_type or "customer_profile").strip().lower()
        if task == "invitation_strategy":
            return self.config.invitation_strategy_api_key
        env = (work_env or "test").strip().lower()
        if env == "production":
            return self.config.api_key_production
        return self.config.api_key_test

    @staticmethod
    def _extract_workflow_run_id(response: httpx.Response, *, timeout_sec: float) -> str | None:
        deadline = time.monotonic() + max(1.0, timeout_sec)
        for line in response.iter_lines():
            if not line:
                continue
            line_text = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line_text.startswith("data:"):
                continue
            data_part = line_text.split("data:", 1)[1].strip()
            if not data_part or data_part == "[DONE]":
                continue
            try:
                event_payload = json.loads(data_part)
            except json.JSONDecodeError:
                continue
            run_id = DifyClient._pick_run_id(event_payload)
            if run_id:
                return run_id
            if time.monotonic() >= deadline:
                break
        # Some deployments may return a JSON body even with streaming mode.
        try:
            fallback = response.json()
        except Exception:
            return None
        if not isinstance(fallback, dict):
            return None
        return DifyClient._pick_run_id(fallback)

    @staticmethod
    def _pick_run_id(payload: dict[str, Any]) -> str | None:
        keys = ("workflow_run_id", "run_id", "task_id")
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    def _build_inputs(
        self,
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

    def _resolve_image_inputs(
        self,
        *,
        client: httpx.Client,
        headers: dict[str, str],
        raw_inputs: dict[str, Any],
        timeout: httpx.Timeout,
    ) -> dict[str, Any]:
        resolved = dict(raw_inputs)
        _log.info("dify_resolve_image_inputs_start image_mode=%s raw_inputs=%s", self.config.image_mode, raw_inputs)
        _log.info("dify_resolve_image_inputs_complete resolved=%s", resolved)
        return resolved

    def _start_streaming_without_sse(
        self,
        *,
        client: httpx.Client,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> str | None:
        response = client.post(self.config.run_url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            return None
        return self._pick_run_id(body)


def extract_output_string(response_body: dict[str, Any]) -> str:
    """Pull the Dify workflow output string out of one of four envelope shapes."""
    candidates: list[Any] = [
        response_body.get("output"),
        response_body.get("data", {}).get("output") if isinstance(response_body.get("data"), dict) else None,
        response_body.get("data", {}).get("outputs", {}).get("output")
        if isinstance(response_body.get("data"), dict)
        and isinstance(response_body.get("data", {}).get("outputs"), dict)
        else None,
        response_body.get("outputs", {}).get("output")
        if isinstance(response_body.get("outputs"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    _log_missing_output_diagnostic(response_body)
    raise DifyOutputInvalidJson("missing output string in dify response")


def _log_missing_output_diagnostic(response_body: Any) -> None:
    """Dump envelope shape + body preview so we can later infer the real Dify response layout.

    Must not raise: this runs on a failure path and any logging error would mask the original problem.
    """
    try:
        body = response_body if isinstance(response_body, dict) else {}
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        outputs = body.get("outputs") if isinstance(body.get("outputs"), dict) else None
        data_outputs = (
            data.get("outputs")
            if isinstance(data, dict) and isinstance(data.get("outputs"), dict)
            else None
        )
        candidates_types = [
            type(body.get("output")).__name__,
            type(data.get("output")).__name__ if data is not None else "NoneType",
            type(data_outputs.get("output")).__name__ if data_outputs is not None else "NoneType",
            type(outputs.get("output")).__name__ if outputs is not None else "NoneType",
        ]
        _log.warning(
            "dify_output_missing_diagnostic root_type=%s body_top_keys=%s "
            "data_top_keys=%s outputs_top_keys=%s data_outputs_top_keys=%s "
            "candidates_types=%s body_preview=%s",
            type(response_body).__name__,
            list(body.keys()),
            list(data.keys()) if data is not None else None,
            list(outputs.keys()) if outputs is not None else None,
            list(data_outputs.keys()) if data_outputs is not None else None,
            candidates_types,
            json.dumps(body, ensure_ascii=False, default=str)[:2000],
        )
    except Exception:
        pass

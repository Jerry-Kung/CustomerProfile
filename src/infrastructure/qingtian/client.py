from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any

import httpx

from src.config.settings import QingTianConfig
from src.infrastructure.qingtian.errors import (
    QingTianOutputInvalidJson,
    QingTianRequestFailed,
    QingTianTimeout,
)

_log = logging.getLogger(__name__)

RUN_PATH = "/run_app_workflow"
QUERY_PATH = "/query_run_app_process"

_NON_TERMINAL_STATUSES = {"to_start", "processing"}
_SUCCESS_STATUS = "success"
_FAILURE_STATUSES = {"failed", "stopped", "interrupted", "timeout"}


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

        _log.info(
            "qingtian_run_workflow_start task_type=%s work_env=%s input_keys=%s",
            task_type,
            work_env,
            sorted(input_data.keys()),
        )

        poll_timeout_sec = max(10.0, self.config.poll_timeout_ms / 1000)
        timeout_submit = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
        timeout_poll = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)

        run_url = f"{self.config.base_url.rstrip('/')}{RUN_PATH}"
        query_url = f"{self.config.base_url.rstrip('/')}{QUERY_PATH}"

        try:
            with httpx.Client(timeout=None) as client:
                run_id = self._submit_workflow(
                    client=client,
                    url=run_url,
                    body=run_body,
                    headers=headers,
                    timeout=timeout_submit,
                )
                if not run_id:
                    raise QingTianRequestFailed("runId missing from /run_app_workflow response")
                _log.info("qingtian_workflow_submitted runId=%s", run_id)
                return self._poll_run_result(
                    client=client,
                    url=query_url,
                    run_id=run_id,
                    api_key=api_key,
                    headers=headers,
                    poll_timeout_sec=poll_timeout_sec,
                    timeout=timeout_poll,
                )
        except httpx.TimeoutException as exc:
            raise QingTianTimeout("qingtian request timed out") from exc
        except httpx.HTTPStatusError as exc:
            body_preview = ""
            try:
                body_preview = (exc.response.text or "")[:800]
            except Exception:
                pass
            raise QingTianRequestFailed(
                f"qingtian HTTP {exc.response.status_code} {exc.request.url}: {body_preview or str(exc)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise QingTianRequestFailed(f"qingtian request failed: {exc}") from exc

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
        payload = response.json()
        if not isinstance(payload, dict):
            raise QingTianRequestFailed("qingtian /run_app_workflow response is not a JSON object")
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
    ) -> str:
        interval_sec = max(0.2, self.config.poll_interval_ms / 1000)
        deadline = time.monotonic() + max(1.0, poll_timeout_sec)
        query_body: dict[str, Any] = {
            "RunID": run_id,
            "UserID": self.config.user_id,
            "AppKey": api_key,
        }
        while True:
            response = client.post(url, json=query_body, headers=headers, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise QingTianRequestFailed("qingtian poll response is not a JSON object")

            status = self._extract_status(body)
            if status == _SUCCESS_STATUS:
                self._log_terminal_response(run_id=run_id, status=status, body=body)
                output = body.get("output")
                if not isinstance(output, str) or not output.strip():
                    raise QingTianOutputInvalidJson(
                        "qingtian success response missing output string"
                    )
                return output

            if status in _FAILURE_STATUSES:
                self._log_terminal_response(run_id=run_id, status=status, body=body)
                message = body.get("message") or body.get("msg") or ""
                code = body.get("code")
                raise QingTianRequestFailed(
                    f"qingtian workflow terminated status={status} code={code} message={message}"
                )

            if status not in _NON_TERMINAL_STATUSES:
                if status not in self._unknown_status_seen:
                    self._unknown_status_seen.add(status)
                    _log.warning(
                        "qingtian_unknown_status runId=%s status=%s body_keys=%s",
                        run_id,
                        status,
                        list(body.keys()),
                    )

            if time.monotonic() >= deadline:
                raise QingTianTimeout("polling qingtian workflow result timed out")
            time.sleep(interval_sec)

    @staticmethod
    def _extract_status(body: dict[str, Any]) -> str:
        value = body.get("status")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _log_terminal_response(*, run_id: str, status: str, body: dict[str, Any]) -> None:
        """Structural diagnostics on terminal QingTian responses (mirrors DifyClient._log_terminal_response)."""
        try:
            nodes = body.get("nodes") if isinstance(body.get("nodes"), dict) else None
            steps = body.get("steps") if isinstance(body.get("steps"), list) else None
            body_json = json.dumps(body, ensure_ascii=False, default=str)
            _log.info(
                "qingtian_poll_terminal_response runId=%s status=%s "
                "body_keys=%s node_count=%s steps_count=%s cost_ms=%s "
                "has_output=%s body_size_bytes=%s",
                run_id,
                status,
                list(body.keys()),
                len(nodes) if nodes is not None else None,
                len(steps) if steps is not None else None,
                body.get("costMs"),
                isinstance(body.get("output"), str),
                len(body_json),
            )
            _log.debug(
                "qingtian_poll_terminal_response_body runId=%s body_preview=%s",
                run_id,
                body_json[:2000],
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

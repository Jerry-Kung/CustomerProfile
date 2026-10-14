from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

from src.config.observability import mask_phone, safe_text
from src.config.settings import ProfileRunnerConfig
from src.infrastructure.runner.errors import RunnerRequestFailed, RunnerTimeout, RunnerWorkflowTerminated
from src.infrastructure.workflow.errors import WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch

WORKFLOW_ID = "customer_profile_entry"
_FAILURE_STATUSES = {"failed", "cancelled", "interrupted", "skipped"}
_RUN_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_log = logging.getLogger(__name__)


def _retry_after(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, math.ceil(seconds)) if math.isfinite(seconds) else None


@dataclass
class RunnerClient:
    config: ProfileRunnerConfig
    transport: httpx.BaseTransport | None = None

    def _base_url(self, work_env: str) -> str:
        if work_env not in {"test", "production"}:
            raise RunnerRequestFailed("runner requires work_env=test or production", stage="submit", status_code=400)
        base_url = getattr(self.config, f"base_url_{work_env}").rstrip("/")
        if not base_url:
            raise RunnerRequestFailed(f"runner URL is not configured for {work_env}", stage="submit", status_code=400)
        return base_url

    def _validate_inputs(self, phone: str | None, batch_id: str | None, task_type: str, *, stage: str):
        if task_type != "customer_profile":
            raise RunnerRequestFailed("runner supports only customer_profile tasks", stage=stage, status_code=400)
        if phone is None or not re.fullmatch(r"1[3-9]\d{9}", phone):
            raise RunnerRequestFailed("runner requires an 11-digit customer phone", stage=stage, status_code=400)
        if batch_id is None or not batch_id or len(batch_id) > 50:
            raise RunnerRequestFailed("runner requires a business batch_id of 1..50 characters", stage=stage, status_code=400)

    def run_workflow(self, *, customer_phonenumber: str, work_env: str = "test",
                     batch_id: str | None = None, task_type: str = "customer_profile") -> str:
        run_id = self.submit_workflow(customer_phonenumber=customer_phonenumber, work_env=work_env,
                                      batch_id=batch_id, task_type=task_type)
        return self.poll_workflow(workflow_run_id=run_id, work_env=work_env, task_type=task_type,
                                  customer_phonenumber=customer_phonenumber, batch_id=batch_id)

    def submit_workflow(self, *, customer_phonenumber: str, work_env: str = "test",
                        batch_id: str | None = None, task_type: str = "customer_profile") -> str:
        self._validate_inputs(customer_phonenumber, batch_id, task_type, stage="submit")
        body = {"workflow_id": WORKFLOW_ID,
                "inputs": {"phone_number": customer_phonenumber, "batch_id": batch_id},
                "business_ref": f"cdp:{customer_phonenumber}:{batch_id}"}
        with httpx.Client(timeout=self.config.request_timeout_ms / 1000, transport=self.transport,
                          follow_redirects=False) as client:
            try:
                response = client.post(self._base_url(work_env) + "/runs", json=body)
            except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise RunnerRequestFailed(f"runner connection not established: {type(exc).__name__}",
                                          stage="submit", retryable=True) from exc
            except httpx.TimeoutException as exc:
                raise RunnerTimeout(f"runner submit acknowledgement lost: {type(exc).__name__}", stage="submit") from exc
            except httpx.HTTPError as exc:
                raise RunnerRequestFailed(f"runner submit acknowledgement unknown: {type(exc).__name__}", stage="submit") from exc
        if response.status_code != 200:
            # Only an explicit 429 proves the queue rejected the submission.
            raise RunnerRequestFailed(f"runner submit returned HTTP {response.status_code}", stage="submit",
                                      status_code=response.status_code, retryable=response.status_code == 429,
                                      retry_after_seconds=_retry_after(response.headers.get("Retry-After")))
        try:
            data = response.json()
            run_id = data["run_id"]
            if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)
                    or data.get("workflow_id") != WORKFLOW_ID):
                raise ValueError("invalid run_id/workflow_id")
        except (ValueError, TypeError, KeyError) as exc:
            raise RunnerRequestFailed("runner submit returned malformed acknowledgement; do not resubmit blindly",
                                      stage="submit") from exc
        _log.info("runner_submitted 说明=画像任务提交成功，运行ID将持久化后轮询 phone=%s batch_id=%s run_id=%s",
                  mask_phone(customer_phonenumber), batch_id, run_id)
        return run_id

    def poll_workflow(self, *, workflow_run_id: str, work_env: str = "test",
                      task_type: str = "customer_profile", customer_phonenumber: str | None = None,
                      batch_id: str | None = None, on_poll: Callable[[], None] | None = None) -> str:
        self._validate_inputs(customer_phonenumber, batch_id, task_type, stage="poll")
        if not _RUN_ID.fullmatch(workflow_run_id):
            raise RunnerRequestFailed("invalid runner run_id", stage="poll", status_code=400)
        deadline = time.monotonic() + self.config.poll_timeout_ms / 1000
        with httpx.Client(transport=self.transport, follow_redirects=False) as client:
            while True:
                if on_poll:
                    on_poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunnerTimeout("runner poll window expired; retain the persisted run_id", stage="poll")
                try:
                    response = client.get(self._base_url(work_env) + "/runs/" + workflow_run_id,
                                          timeout=min(self.config.request_timeout_ms / 1000, remaining))
                except httpx.TimeoutException as exc:
                    raise RunnerTimeout(f"runner query timed out: {type(exc).__name__}", stage="poll") from exc
                except httpx.HTTPError as exc:
                    raise RunnerRequestFailed(f"runner query failed: {type(exc).__name__}", stage="poll", retryable=True) from exc
                if response.status_code != 200:
                    raise RunnerRequestFailed(f"runner query returned HTTP {response.status_code}", stage="poll",
                                              status_code=response.status_code, retryable=True,
                                              retry_after_seconds=_retry_after(response.headers.get("Retry-After")))
                try:
                    detail = response.json()
                    if (not isinstance(detail, dict) or detail.get("run_id") != workflow_run_id
                            or detail.get("workflow_id") != WORKFLOW_ID or detail.get("is_replay") is not False
                            or detail.get("business_ref") != f"cdp:{customer_phonenumber}:{batch_id}"
                            or detail.get("inputs", {}).get("phone_number") != customer_phonenumber
                            or detail.get("inputs", {}).get("batch_id") != batch_id):
                        raise ValueError("run identity, workflow, phone, batch or replay mismatch")
                except (ValueError, TypeError, AttributeError) as exc:
                    raise RunnerRequestFailed("runner detail identity is invalid", stage="poll") from exc
                status = detail.get("status")
                if status == "succeeded":
                    return self._analysis_result(detail, customer_phonenumber, batch_id)
                if status in _FAILURE_STATUSES:
                    if status in {"cancelled", "interrupted"}:
                        raise RunnerRequestFailed("runner stopped; reconcile side effects before retrying",
                                                  stage="reconcile")
                    raise RunnerWorkflowTerminated(safe_text(str(detail.get("error") or status), 800), terminal_status=status)
                if status not in {"queued", "running"}:
                    raise RunnerRequestFailed(f"runner reported unknown status: {safe_text(str(status), 80)}", stage="poll")
                if on_poll:
                    on_poll()
                time.sleep(min(self.config.poll_interval_ms / 1000, max(0, deadline - time.monotonic())))

    def _analysis_result(self, detail, phone, batch_id) -> str:
        try:
            result = json.loads(detail.get("outputs", {}).get("result1", ""))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkflowOutputInvalidJson("runner result1 is not a JSON string") from exc
        if (not isinstance(result, dict) or result.get("phone") != phone or result.get("batch_id") != batch_id
                or not isinstance(result.get("analysis_result"), dict)):
            raise WorkflowOutputSchemaMismatch("runner result identity or analysis_result object is invalid")
        return json.dumps(result["analysis_result"], ensure_ascii=False)

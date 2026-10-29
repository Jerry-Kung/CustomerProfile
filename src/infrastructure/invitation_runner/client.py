from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from src.config.observability import mask_phone, safe_text
from src.config.settings import InvitationRunnerConfig
from src.infrastructure.invitation_runner.errors import (
    InvitationRunnerRequestFailed,
    InvitationRunnerTimeout,
    InvitationRunnerWorkflowTerminated,
)
from src.infrastructure.runner.client import _retry_after

WORKFLOW_ID = "invitation_strategy"
TASK_PATH = "/api/v1/invitation-tasks"
_log = logging.getLogger(__name__)


def invitation_run_id(phone: str, batch_id: str, execution_id: str) -> str:
    # Matches CustomerProfile.invitation_inbound.invitation_run_id exactly,
    # including JSON's default separators and non-ASCII batch identifiers.
    identity = json.dumps([phone, batch_id, execution_id], ensure_ascii=False)
    return "inv_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass
class InvitationRunnerClient:
    config: InvitationRunnerConfig
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def _body(self, phone, batch_id, execution_id, work_env, task_type, *, stage):
        if (
            task_type != WORKFLOW_ID
            or work_env not in {"test", "production"}
            or not isinstance(phone, str)
            or not re.fullmatch(r"1[3-9][0-9]{9}", phone)
            or not isinstance(batch_id, str)
            or not 1 <= len(batch_id) <= 50
            or batch_id != batch_id.strip()
            or not isinstance(execution_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", execution_id)
        ):
            raise InvitationRunnerRequestFailed(
                "native invitation requires invitation_strategy, a valid phone/batch/execution and test/production environment",
                stage=stage,
                status_code=422,
            )
        return {
            "phone_number": phone,
            "batch_id": batch_id,
            "execution_id": execution_id,
            "work_env": work_env,
        }

    def _connection(self, work_env, *, stage):
        base_url = getattr(self.config, f"base_url_{work_env}").rstrip("/")
        key = getattr(self.config, f"api_key_{work_env}")
        if not base_url or not key:
            raise InvitationRunnerRequestFailed(
                f"native invitation URL/key is not configured for {work_env}",
                stage=stage,
                status_code=422,
            )
        return base_url, {"X-API-Key": key}

    def _validate_detail(self, detail, body, run_id):
        if (
            not isinstance(detail, dict)
            or detail.get("run_id") != run_id
            or detail.get("workflow_id") != WORKFLOW_ID
            or any(detail.get(key) != value for key, value in body.items())
            or ("is_replay" in detail and detail["is_replay"] is not False)
        ):
            raise InvitationRunnerRequestFailed(
                "native invitation run identity is invalid", stage="reconcile"
            )

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = WORKFLOW_ID,
        execution_id: str | None = None,
    ) -> str:
        run_id = self.submit_workflow(
            customer_phonenumber=customer_phonenumber,
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
            execution_id=execution_id,
        )
        return self.poll_workflow(
            workflow_run_id=run_id,
            customer_phonenumber=customer_phonenumber,
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
            execution_id=execution_id,
        )

    def submit_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = WORKFLOW_ID,
        execution_id: str | None = None,
    ) -> str:
        body = self._body(
            customer_phonenumber,
            batch_id,
            execution_id,
            work_env,
            task_type,
            stage="submit",
        )
        run_id = invitation_run_id(customer_phonenumber, batch_id, execution_id)
        base_url, headers = self._connection(work_env, stage="submit")
        with httpx.Client(
            timeout=self.config.request_timeout_ms / 1000,
            transport=self.transport,
            follow_redirects=False,
            headers=headers,
        ) as client:
            try:
                response = client.post(base_url + TASK_PATH, json=body)
            except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise InvitationRunnerRequestFailed(
                    "native invitation connection was not established",
                    stage="submit",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                if self._recover_acknowledgement(client, base_url, body, run_id):
                    return run_id
                raise InvitationRunnerTimeout(
                    "native invitation acknowledgement is unknown; reconcile this execution before resubmitting",
                    stage="submit",
                ) from exc
            if response.status_code != 200:
                if response.status_code >= 500 or response.status_code == 408:
                    if self._recover_acknowledgement(client, base_url, body, run_id):
                        return run_id
                raise InvitationRunnerRequestFailed(
                    f"native invitation submit returned HTTP {response.status_code}",
                    stage="reconcile" if response.status_code == 409 else "submit",
                    status_code=response.status_code,
                    retryable=response.status_code == 429,
                    retry_after_seconds=_retry_after(
                        response.headers.get("Retry-After")
                    ),
                )
            try:
                acknowledgement = response.json()
                if (
                    not isinstance(acknowledgement, dict)
                    or acknowledgement.get("status") != "accepted"
                    or acknowledgement.get("workflow_id") != WORKFLOW_ID
                    or acknowledgement.get("run_id") != run_id
                    or any(
                        acknowledgement.get(key) != value for key, value in body.items()
                    )
                ):
                    raise ValueError("acknowledgement identity mismatch")
            except (ValueError, TypeError):
                if not self._recover_acknowledgement(client, base_url, body, run_id):
                    raise InvitationRunnerRequestFailed(
                        "native invitation acknowledgement cannot be verified",
                        stage="submit",
                    ) from None
        _log.info(
            "invitation_runner_submitted phone=%s batch_id=%s run_id=%s",
            mask_phone(customer_phonenumber),
            batch_id,
            run_id,
        )
        return run_id

    def _recover_acknowledgement(self, client, base_url, body, run_id):
        # One authenticated read may recover a lost POST response. Never POST
        # again to guess whether an uncertain submission already took effect.
        try:
            response = client.get(base_url + TASK_PATH + "/" + run_id)
            if response.status_code != 200:
                return False
            detail = response.json()
            self._validate_detail(detail, body, run_id)
            if detail.get("status") not in {
                "queued",
                "running",
                "succeeded",
                "failed",
                "interrupted",
                "cancelled",
            }:
                return False
        except (httpx.HTTPError, ValueError, InvitationRunnerRequestFailed):
            return False
        _log.info(
            "invitation_runner_ack_recovered phone=%s run_id=%s",
            mask_phone(body["phone_number"]),
            run_id,
        )
        return True

    def poll_workflow(
        self,
        *,
        workflow_run_id: str,
        work_env: str = "test",
        task_type: str = WORKFLOW_ID,
        customer_phonenumber: str | None = None,
        batch_id: str | None = None,
        execution_id: str | None = None,
        on_poll: Callable[[], None] | None = None,
    ) -> str:
        body = self._body(
            customer_phonenumber,
            batch_id,
            execution_id,
            work_env,
            task_type,
            stage="poll",
        )
        if workflow_run_id != invitation_run_id(
            customer_phonenumber, batch_id, execution_id
        ):
            raise InvitationRunnerRequestFailed(
                "native invitation run_id does not match its execution",
                stage="reconcile",
            )
        base_url, headers = self._connection(work_env, stage="poll")
        deadline = time.monotonic() + self.config.poll_timeout_ms / 1000
        with httpx.Client(
            transport=self.transport, follow_redirects=False, headers=headers
        ) as client:
            while True:
                if on_poll:
                    on_poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InvitationRunnerTimeout(
                        "native invitation poll window expired; retain its run_id",
                        stage="poll",
                    )
                try:
                    response = client.get(
                        base_url + TASK_PATH + "/" + workflow_run_id,
                        timeout=min(self.config.request_timeout_ms / 1000, remaining),
                    )
                except httpx.HTTPError as exc:
                    raise InvitationRunnerRequestFailed(
                        "native invitation status read failed",
                        stage="poll",
                        retryable=True,
                    ) from exc
                if response.status_code != 200:
                    raise InvitationRunnerRequestFailed(
                        f"native invitation status returned HTTP {response.status_code}",
                        stage="poll",
                        status_code=response.status_code,
                        retryable=True,
                        retry_after_seconds=_retry_after(
                            response.headers.get("Retry-After")
                        ),
                    )
                try:
                    detail = response.json()
                except ValueError:
                    raise InvitationRunnerRequestFailed(
                        "native invitation status is not JSON",
                        stage="poll",
                        retryable=True,
                    ) from None
                self._validate_detail(detail, body, workflow_run_id)
                status = detail.get("status")
                delivery = detail.get("delivery")
                if status == "succeeded":
                    if delivery != "confirmed":
                        raise InvitationRunnerRequestFailed(
                            "native invitation did not confirm business delivery",
                            stage="reconcile",
                        )
                    return self._result(detail, body)
                if status == "failed" and delivery == "not_attempted":
                    raise InvitationRunnerWorkflowTerminated(
                        safe_text(
                            str(
                                detail.get("error")
                                or "native invitation failed before writeback"
                            ),
                            800,
                        ),
                        terminal_status=status,
                    )
                if status in {"failed", "interrupted", "cancelled", "skipped"}:
                    raise InvitationRunnerRequestFailed(
                        "native invitation stopped with uncertain side effects",
                        stage="reconcile",
                    )
                if status not in {"queued", "running"} or delivery != "pending":
                    raise InvitationRunnerRequestFailed(
                        "native invitation reported an invalid state", stage="reconcile"
                    )
                if on_poll:
                    on_poll()
                time.sleep(
                    min(
                        self.config.poll_interval_ms / 1000,
                        max(0, deadline - time.monotonic()),
                    )
                )

    def _result(self, detail, body):
        try:
            result = json.loads(detail.get("outputs", {}).get("result", ""))
        except (ValueError, TypeError, AttributeError):
            raise InvitationRunnerRequestFailed(
                "native invitation result is not valid JSON", stage="reconcile"
            ) from None
        if (
            not isinstance(result, dict)
            or result.get("phone") != body["phone_number"]
            or result.get("batch_id") != body["batch_id"]
            or result.get("execution_id") != body["execution_id"]
            or not isinstance(result.get("script"), dict)
            or not result["script"]
            or not isinstance(result.get("audit"), dict)
            or not result["audit"]
            or not isinstance(result.get("follow_up_items"), list)
            or any(not isinstance(item, dict) for item in result["follow_up_items"])
            or not isinstance(result.get("follow_up_summary"), str)
        ):
            raise InvitationRunnerRequestFailed(
                "native invitation result identity or envelope is invalid",
                stage="reconcile",
            )
        return json.dumps(result, ensure_ascii=False)

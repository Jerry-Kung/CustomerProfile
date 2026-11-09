"""Confirm invitation persistence independently of the remote workflow result."""

import httpx

from src.config.settings import FailureCallbackConfig
from src.infrastructure.workflow.errors import WorkflowError


class InvitationWritebackMissing(WorkflowError):
    code = "INVITATION_WRITEBACK_MISSING"


class InvitationWritebackUnconfirmed(WorkflowError):
    code = "INVITATION_REQUIRES_RECONCILIATION"


class InvitationWritebackSuperseded(InvitationWritebackUnconfirmed):
    pass


class InvitationWritebackVerifier:
    def __init__(self, config: FailureCallbackConfig, *, transport=None):
        self._config = config
        self._transport = transport

    def verify(self, *, phone: str, batch_id: str | None, execution_id: str, work_env: str) -> None:
        if work_env not in {"test", "production"}:
            raise InvitationWritebackUnconfirmed("unsupported invitation environment")
        base_url = getattr(self._config, f"base_url_{work_env}")
        api_key = getattr(self._config, f"api_key_{work_env}")
        if not self._config.enabled or not base_url or not api_key:
            raise InvitationWritebackUnconfirmed("invitation status URL/key or failure delivery is not configured")
        try:
            with httpx.Client(timeout=self._config.timeout_seconds, transport=self._transport) as client:
                response = client.get(
                    f"{base_url.rstrip('/')}/api/v1/remote/data/invitation/{phone}/status",
                    headers={"X-API-Key": api_key},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Retain the known remote run; a failed status read must never cause
            # another workflow submission or a terminal failure notification.
            raise InvitationWritebackUnconfirmed(f"invitation status read failed: {type(exc).__name__}") from exc
        if (not isinstance(data, dict) or data.get("phone") != phone
                or type(data.get("analysis_status")) is not int
                or data["analysis_status"] not in {-1, 0, 1}
                or any(type(data.get(key)) is not bool for key in ("has_script", "has_audit"))
                or "batch_id" not in data or "execution_id" not in data):
            raise InvitationWritebackUnconfirmed("invalid invitation status response")
        if data["batch_id"] != batch_id or data["execution_id"] != execution_id:
            raise InvitationWritebackSuperseded("invitation execution has been superseded")
        if data["analysis_status"] == 0 and data["has_script"] and data["has_audit"]:
            return
        raise InvitationWritebackMissing("workflow succeeded but current invitation script/audit was not committed")

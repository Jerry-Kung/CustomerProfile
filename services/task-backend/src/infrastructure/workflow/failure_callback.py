import httpx

from src.config.settings import FailureCallbackConfig
from src.domain.task.failure_callback import TaskFailure


class TaskFailureClient:
    def __init__(self, config: FailureCallbackConfig, *, transport=None):
        self._config = config
        self._transport = transport

    def send(self, failure: TaskFailure) -> None:
        if failure.work_env not in {"test", "production"}:
            raise ValueError("unsupported task callback environment")
        suffix = failure.work_env
        base_url = getattr(self._config, f"base_url_{suffix}")
        api_key = getattr(self._config, f"api_key_{suffix}")
        if not base_url or not api_key:
            raise ValueError("task failure callback URL/key is missing")
        endpoints = {"customer_profile": "analysis-failed", "invitation_strategy": "invitation-failed"}
        if failure.task_type not in endpoints:
            raise ValueError("unsupported failure callback task type")
        payload = {"phone": failure.phone, "batch_id": failure.batch_id,
                   "error_msg": failure.error_message[:1000]}
        if failure.execution_id:
            payload["execution_id"] = failure.execution_id
        if failure.task_type == "invitation_strategy":
            payload["final"] = True
        with httpx.Client(timeout=self._config.timeout_seconds, transport=self._transport) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/v1/remote/callback/{endpoints[failure.task_type]}",
                headers={"X-API-Key": api_key},
                json=payload,
            )
            response.raise_for_status()


ProfileFailureClient = TaskFailureClient

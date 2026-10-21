import httpx

from src.config.settings import FailureCallbackConfig
from src.domain.task.failure_callback import ProfileFailure


class ProfileFailureClient:
    def __init__(self, config: FailureCallbackConfig, *, transport=None):
        self._config = config
        self._transport = transport

    def send(self, failure: ProfileFailure) -> None:
        if failure.work_env not in {"test", "production"}:
            raise ValueError("unsupported profile callback environment")
        suffix = failure.work_env
        base_url = getattr(self._config, f"base_url_{suffix}")
        api_key = getattr(self._config, f"api_key_{suffix}")
        if not base_url or not api_key:
            raise ValueError("profile failure callback URL/key is missing")
        with httpx.Client(timeout=self._config.timeout_seconds, transport=self._transport) as client:
            response = client.post(
                f"{base_url}/api/v1/remote/callback/analysis-failed",
                headers={"X-API-Key": api_key},
                json={"phone": failure.phone, "batch_id": failure.batch_id,
                      "error_msg": failure.error_message[:1000]},
            )
            response.raise_for_status()

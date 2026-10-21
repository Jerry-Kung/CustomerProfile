from threading import Event

from src.config.logging_utils import get_logger
from src.domain.task.failure_callback import FailureCallbackRepository
from src.infrastructure.workflow.failure_callback import ProfileFailureClient


class FailureCallbackDispatcher:
    def __init__(self, repository: FailureCallbackRepository, client: ProfileFailureClient):
        self._repository = repository
        self._client = client
        self._logger = get_logger("profile_failure_callbacks")

    def run_once(self) -> bool:
        self._repository.enqueue_failed()
        return self._repository.deliver_next(self._client.send)

    def run(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            try:
                delivered = self.run_once()
            except Exception:
                # Exception text may contain a credential-bearing provider URL.
                self._logger.warning("profile_failure_callback_pending_retry")
                delivered = False
            stop_event.wait(0.1 if delivered else 5)

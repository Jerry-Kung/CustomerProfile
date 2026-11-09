from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class TaskFailure:
    phone: str
    batch_id: str
    work_env: str
    error_message: str
    task_type: str = "customer_profile"
    execution_id: str | None = None


# Preserve imports used by existing profile integrations.
ProfileFailure = TaskFailure


class FailureCallbackRepository(Protocol):
    def enqueue_failed(self) -> None: ...

    def deliver_next(self, send: Callable[[TaskFailure], None]) -> bool: ...

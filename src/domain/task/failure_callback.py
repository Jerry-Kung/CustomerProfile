from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class ProfileFailure:
    phone: str
    batch_id: str
    work_env: str
    error_message: str


class FailureCallbackRepository(Protocol):
    def enqueue_failed(self) -> None: ...

    def deliver_next(self, send: Callable[[ProfileFailure], None]) -> bool: ...

from __future__ import annotations

from typing import Protocol

from src.domain.task.models import TaskRecord


class TaskRepository(Protocol):
    def upsert_queued(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> TaskRecord:
        ...

    def claim_next_queued(self) -> TaskRecord | None:
        ...

    def count_queued(self) -> int:
        ...

    def list_by_prefix(
        self,
        *,
        phone_prefix: str,
        page: int,
        page_size: int,
        task_type: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        ...

    def get_by_phone(self, customer_phonenumber: str, *, task_type: str) -> TaskRecord | None:
        ...

    def mark_succeeded(self, *, task_id: int, output: dict, card_id: str, nickname: str) -> None:
        ...

    def mark_failed(self, *, task_id: int, error_message: str) -> None:
        ...

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int) -> int:
        ...


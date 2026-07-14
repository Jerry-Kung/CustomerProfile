from __future__ import annotations

from dataclasses import dataclass
import logging

from src.domain.task.models import TaskRecord
from src.domain.task.repository import TaskRepository


@dataclass
class TaskService:
    repository: TaskRepository
    logger: logging.Logger

    def submit_task(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> TaskRecord:
        record = self.repository.upsert_queued(
            customer_phonenumber,
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
        )
        self.logger.info(
            "task_submitted customer_phonenumber=%s task_type=%s work_env=%s batch_id=%s status=%s",
            customer_phonenumber,
            task_type,
            work_env,
            batch_id,
            record.status,
        )
        return record

    def claim_next_task(self) -> TaskRecord | None:
        record = self.repository.claim_next_queued()
        if record:
            self.logger.info(
                "task_claimed id=%s customer_phonenumber=%s task_type=%s status=%s",
                record.id,
                record.customer_phonenumber,
                record.task_type,
                record.status,
            )
        return record

    def list_tasks(
        self,
        *,
        phone_prefix: str,
        page: int,
        page_size: int,
        task_type: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        items, total = self.repository.list_by_prefix(
            phone_prefix=phone_prefix,
            page=page,
            page_size=page_size,
            task_type=task_type,
        )
        self.logger.info(
            "task_listed prefix=%s task_type=%s page=%s page_size=%s total=%s",
            phone_prefix,
            task_type,
            page,
            page_size,
            total,
        )
        return items, total

    def get_task_by_phone(
        self,
        customer_phonenumber: str,
        *,
        task_type: str = "customer_profile",
    ) -> TaskRecord | None:
        record = self.repository.get_by_phone(customer_phonenumber, task_type=task_type)
        if record:
            self.logger.info(
                "task_fetched customer_phonenumber=%s task_type=%s status=%s",
                customer_phonenumber,
                task_type,
                record.status,
            )
        else:
            self.logger.info(
                "task_not_found customer_phonenumber=%s task_type=%s",
                customer_phonenumber,
                task_type,
            )
        return record

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int = 100) -> int:
        affected = self.repository.requeue_stale_processing(timeout_seconds=timeout_seconds, limit=limit)
        if affected > 0:
            self.logger.warning(
                "task_requeued_stale_processing timeout_seconds=%s limit=%s affected=%s",
                timeout_seconds,
                limit,
                affected,
            )
        return affected

from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from src.config.observability import elapsed_ms, log_context, mask_phone
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
        execution_id: str | None = None,
    ) -> TaskRecord:
        start = time.perf_counter()
        with log_context(batch_id=batch_id, task_type=task_type, work_env=work_env):
            try:
                record = self.repository.upsert_queued(
                    customer_phonenumber,
                    work_env=work_env,
                    batch_id=batch_id,
                    task_type=task_type,
                    **({"execution_id": execution_id} if execution_id is not None else {}),
                )
            except Exception:
                self.logger.exception(
                    "task_submit_failed customer_phone_masked=%s elapsed_ms=%s",
                    mask_phone(customer_phonenumber),
                    elapsed_ms(start),
                )
                raise
            with log_context(task_id=record.id):
                self.logger.info(
                    "task_submitted customer_phone_masked=%s status=%s elapsed_ms=%s",
                    mask_phone(customer_phonenumber),
                    record.status,
                    elapsed_ms(start),
                )
        return record

    def claim_next_task(
        self,
        *,
        retry_enabled: bool = False,
        lease_seconds: int | None = None,
        execution_pool: str = "local",
        workflow_target_id: str = "local-default",
        workflow_target_revision: str = "v1",
        allow_new_tasks: bool = True,
        allow_unbound_retries: bool = True,
        task_types: tuple[str, ...] = ("customer_profile", "invitation_strategy"),
        work_envs: tuple[str, ...] = ("test", "production"),
        require_profile_identity: bool = False,
        require_invitation_identity: bool = False,
    ) -> TaskRecord | None:
        claim_eligible = getattr(self.repository, "claim_next_eligible", None)
        if retry_enabled and lease_seconds is not None and callable(claim_eligible):
            record = claim_eligible(
                lease_seconds=lease_seconds,
                execution_pool=execution_pool,
                workflow_target_id=workflow_target_id,
                workflow_target_revision=workflow_target_revision,
                allow_new_tasks=allow_new_tasks,
                allow_unbound_retries=allow_unbound_retries,
                task_types=task_types,
                work_envs=work_envs,
                **({"require_profile_identity": True} if require_profile_identity else {}),
                **({"require_invitation_identity": True} if require_invitation_identity else {}),
            )
        else:
            # A non-resumable target cannot drain bound work or claim outside
            # its declared task/environment capabilities.
            if not allow_new_tasks:
                return None
            record = self.repository.claim_next_queued(
                task_types=task_types,
                work_envs=work_envs,
            )
        if record:
            with log_context(
                task_id=record.id,
                batch_id=record.batch_id,
                task_type=record.task_type,
                work_env=record.work_env,
            ):
                self.logger.info(
                    "task_claimed customer_phone_masked=%s status=%s",
                    mask_phone(record.customer_phonenumber),
                    record.status,
                )
        return record

    def recover_expired_retry_leases(self, *, limit: int = 100, workflow_target_id: str | None = None,
                                    workflow_target_revision: str | None = None) -> tuple[int, int]:
        recover = getattr(self.repository, "recover_expired_retry_leases", None)
        if not callable(recover):
            return 0, 0
        kwargs = {"limit": limit}
        if workflow_target_id is not None or workflow_target_revision is not None:
            kwargs.update(workflow_target_id=workflow_target_id, workflow_target_revision=workflow_target_revision)
        recoverable, unknown = recover(**kwargs)
        if recoverable or unknown:
            self.logger.warning(
                "task_retry_lease_recovered recoverable_tasks=%s unknown_submissions_failed=%s limit=%s",
                recoverable,
                unknown,
                limit,
            )
        return recoverable, unknown


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
            "task_listed phone_prefix_masked=%s task_type=%s page=%s page_size=%s total=%s",
            mask_phone(phone_prefix),
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
                "task_fetched task_id=%s customer_phone_masked=%s task_type=%s status=%s",
                record.id,
                mask_phone(customer_phonenumber),
                task_type,
                record.status,
            )
        else:
            self.logger.info(
                "task_not_found customer_phone_masked=%s task_type=%s",
                mask_phone(customer_phonenumber),
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

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
        execution_id: str | None = None,
    ) -> TaskRecord:
        ...

    def claim_next_queued(
        self,
        *,
        task_types: tuple[str, ...] | None = None,
        work_envs: tuple[str, ...] | None = None,
    ) -> TaskRecord | None:
        ...

    def claim_next_eligible(
        self,
        *,
        lease_seconds: int,
        execution_pool: str,
        workflow_target_id: str,
        workflow_target_revision: str,
        allow_new_tasks: bool,
        allow_unbound_retries: bool,
        task_types: tuple[str, ...],
        work_envs: tuple[str, ...],
        require_profile_identity: bool = False,
    ) -> TaskRecord | None:
        ...

    def count_queued(self, *, task_type: str | None = None) -> int:
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

    def mark_succeeded(
        self,
        *,
        task_id: int,
        output: dict,
        card_id: str | None,
        nickname: str | None,
    ) -> None:
        ...

    def mark_failed(self, *, task_id: int, error_message: str) -> None:
        ...

    def reserve_workflow_attempt(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        max_attempts: int,
        execution_pool: str,
        workflow_target_id: str,
        workflow_target_revision: str,
        retry_policy_version: str,
        max_pool_attempts: int,
        max_target_attempts: int,
        route_reason: str,
    ) -> int | None:
        ...


    def persist_workflow_run(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int,
        workflow_run_id: str,
    ) -> bool:
        ...

    def refresh_lease(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        ...

    def mark_succeeded_claimed(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        output: dict,
        card_id: str | None,
        nickname: str | None,
        attempt_no: int | None,
    ) -> bool:
        ...

    def mark_failed_claimed(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        error_code: str,
        error_stage: str,
        error_message: str,
        attempt_no: int | None,
    ) -> bool:
        ...

    def schedule_retry(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        error_code: str,
        error_stage: str,
        error_message: str,
        delay_seconds: int,
        retain_workflow_run: bool,
        terminal_attempt: bool,
        attempt_no: int | None,
        increment_poll_recovery: bool = False,
        next_execution_pool: str | None = None,
    ) -> bool:
        ...

    def mark_stalled_if_run_lifetime_exceeded(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
        max_run_lifetime_seconds: int,
    ) -> bool:
        ...

    def recover_expired_retry_leases(self, *, limit: int, workflow_target_id: str | None = None,
                                    workflow_target_revision: str | None = None) -> tuple[int, int]:
        ...

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int) -> int:
        ...


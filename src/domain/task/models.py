from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskRecord:
    id: int | None
    customer_phonenumber: str
    status: str
    updated_at: str | None = None
    output: dict | None = None
    card_id: str | None = None
    nickname: str | None = None
    error_message: str | None = None
    work_env: str = "test"
    batch_id: str | None = None
    task_type: str = "customer_profile"
    generation: int = 1
    execution_backend: str = "local"
    workflow_run_id: str | None = None
    attempt_count: int = 0
    poll_recovery_count: int = 0
    workflow_run_started_at: str | None = None
    next_retry_at: str | None = None
    last_error_code: str | None = None
    last_error_stage: str | None = None
    lease_expires_at: str | None = None
    claim_token: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


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


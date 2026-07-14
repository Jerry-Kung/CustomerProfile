from __future__ import annotations

from typing import Protocol


class WorkflowClient(Protocol):
    """Platform-agnostic workflow client.

    Implementations submit a workflow run and block until the run reaches a terminal
    state. The returned string is the workflow's raw output payload (a JSON string for
    the customer-profile workflow); envelope unwrapping is each implementation's
    responsibility so the caller stays platform-agnostic.
    """

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str,
        batch_id: str | None,
        task_type: str,
    ) -> str: ...

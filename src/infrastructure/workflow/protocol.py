from __future__ import annotations

from typing import Callable, Protocol


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


class ResumableWorkflowClient(WorkflowClient, Protocol):
    """Separate submission from polling so a remote run id can be persisted."""

    def submit_workflow(self, *, customer_phonenumber: str, work_env: str,
                        batch_id: str | None, task_type: str) -> str: ...

    def poll_workflow(self, *, workflow_run_id: str, work_env: str, task_type: str,
                      customer_phonenumber: str | None = None, batch_id: str | None = None,
                      on_poll: Callable[[], None] | None = None) -> str: ...

from __future__ import annotations

from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    WorkflowRequestFailed,
    WorkflowTimeout,
)


class QingTianError(WorkflowError):
    code: str = "QINGTIAN_REQUEST_FAILED"


class QingTianRequestFailed(QingTianError, WorkflowRequestFailed):
    code = "QINGTIAN_REQUEST_FAILED"

    def __init__(
        self,
        message: str,
        *,
        stage: str = "request",
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        retryable: bool = False,
    ):
        self.stage = stage
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable
        super().__init__(message)


class QingTianTimeout(QingTianError, WorkflowTimeout):
    code = "QINGTIAN_TIMEOUT"

    def __init__(self, message: str, *, stage: str = "request"):
        self.stage = stage
        # A submit timeout is deliberately not retryable by default: the remote
        # workflow may already have been created but its runId response lost.
        self.retryable = False
        super().__init__(message)


class QingTianOutputInvalidJson(QingTianError, WorkflowOutputInvalidJson):
    code = "QINGTIAN_OUTPUT_INVALID_JSON"


class QingTianWorkflowTerminated(QingTianRequestFailed):
    """A known QingTian run reached a failed terminal state."""

    code = "QINGTIAN_WORKFLOW_FAILED"

    def __init__(self, message: str, *, terminal_status: str):
        self.terminal_status = terminal_status
        super().__init__(message, stage="workflow", retryable=True)

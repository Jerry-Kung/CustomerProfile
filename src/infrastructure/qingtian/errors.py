from __future__ import annotations

from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    ResumableWorkflowRequestFailed,
    ResumableWorkflowTimeout,
    WorkflowTerminated,
)


class QingTianError(WorkflowError):
    code: str = "QINGTIAN_REQUEST_FAILED"


class QingTianRequestFailed(QingTianError, ResumableWorkflowRequestFailed):
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
        ResumableWorkflowRequestFailed.__init__(
            self, message, stage=stage, status_code=status_code,
            retry_after_seconds=retry_after_seconds, retryable=retryable,
        )


class QingTianTimeout(QingTianError, ResumableWorkflowTimeout):
    code = "QINGTIAN_TIMEOUT"

    def __init__(self, message: str, *, stage: str = "request"):
        # A lost submit response must never trigger a blind second submission.
        ResumableWorkflowTimeout.__init__(self, message, stage=stage)


class QingTianOutputInvalidJson(QingTianError, WorkflowOutputInvalidJson):
    code = "QINGTIAN_OUTPUT_INVALID_JSON"


class QingTianWorkflowTerminated(QingTianRequestFailed, WorkflowTerminated):
    """A known QingTian run reached a failed terminal state."""

    code = "QINGTIAN_WORKFLOW_FAILED"

    def __init__(self, message: str, *, terminal_status: str):
        self.terminal_status = terminal_status
        QingTianRequestFailed.__init__(self, message, stage="workflow", retryable=True)

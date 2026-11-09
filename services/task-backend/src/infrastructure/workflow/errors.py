from __future__ import annotations


class WorkflowError(Exception):
    """Base class for workflow-platform errors. `code` is persisted to tasks.error_message."""

    code: str = "WORKFLOW_REQUEST_FAILED"


class WorkflowRequestFailed(WorkflowError):
    code = "WORKFLOW_REQUEST_FAILED"


class WorkflowTimeout(WorkflowError):
    code = "WORKFLOW_TIMEOUT"


class WorkflowOutputInvalidJson(WorkflowError):
    code = "WORKFLOW_OUTPUT_INVALID_JSON"


class WorkflowOutputSchemaMismatch(WorkflowError):
    code = "WORKFLOW_OUTPUT_SCHEMA_MISMATCH"


class ResumableWorkflowRequestFailed(WorkflowRequestFailed):
    def __init__(self, message: str, *, stage: str = "request", status_code: int | None = None,
                 retry_after_seconds: int | None = None, retryable: bool = False):
        self.stage = stage
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable
        super().__init__(message)


class ResumableWorkflowTimeout(WorkflowTimeout):
    def __init__(self, message: str, *, stage: str = "request"):
        self.stage = stage
        self.retryable = False
        super().__init__(message)


class WorkflowTerminated(ResumableWorkflowRequestFailed):
    """A known remote run reached a failed terminal state."""

    code = "WORKFLOW_FAILED"

    def __init__(self, message: str, *, terminal_status: str, retryable: bool = True):
        self.terminal_status = terminal_status
        super().__init__(message, stage="workflow", retryable=retryable)

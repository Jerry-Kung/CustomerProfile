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

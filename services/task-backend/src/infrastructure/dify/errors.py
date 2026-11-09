from __future__ import annotations

from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
    WorkflowRequestFailed,
    WorkflowTimeout,
)


class DifyError(WorkflowError):
    code: str = "DIFY_REQUEST_FAILED"


class DifyRequestFailed(DifyError, WorkflowRequestFailed):
    code = "DIFY_REQUEST_FAILED"


class DifyTimeout(DifyError, WorkflowTimeout):
    code = "DIFY_TIMEOUT"


class DifyOutputInvalidJson(DifyError, WorkflowOutputInvalidJson):
    code = "DIFY_OUTPUT_INVALID_JSON"


class DifyOutputSchemaMismatch(DifyError, WorkflowOutputSchemaMismatch):
    code = "DIFY_OUTPUT_SCHEMA_MISMATCH"

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


class QingTianTimeout(QingTianError, WorkflowTimeout):
    code = "QINGTIAN_TIMEOUT"


class QingTianOutputInvalidJson(QingTianError, WorkflowOutputInvalidJson):
    code = "QINGTIAN_OUTPUT_INVALID_JSON"

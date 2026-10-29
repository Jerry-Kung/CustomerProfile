from src.infrastructure.workflow.errors import (
    ResumableWorkflowRequestFailed,
    ResumableWorkflowTimeout,
    WorkflowTerminated,
)


class InvitationRunnerRequestFailed(ResumableWorkflowRequestFailed):
    code = "INVITATION_RUNNER_REQUEST_FAILED"


class InvitationRunnerTimeout(ResumableWorkflowTimeout):
    code = "INVITATION_RUNNER_TIMEOUT"


class InvitationRunnerWorkflowTerminated(WorkflowTerminated):
    code = "INVITATION_RUNNER_WORKFLOW_FAILED"

    def __init__(self, message: str, *, terminal_status: str):
        # The service has already exhausted its model retries. Reposting the
        # same execution returns this immutable terminal run, never a new one.
        super().__init__(message, terminal_status=terminal_status, retryable=False)

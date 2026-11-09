from src.infrastructure.workflow.errors import (
    ResumableWorkflowRequestFailed, ResumableWorkflowTimeout, WorkflowTerminated,
)


class RunnerRequestFailed(ResumableWorkflowRequestFailed):
    code = "RUNNER_REQUEST_FAILED"


class RunnerTimeout(ResumableWorkflowTimeout):
    code = "RUNNER_TIMEOUT"


class RunnerWorkflowTerminated(WorkflowTerminated):
    code = "RUNNER_WORKFLOW_FAILED"

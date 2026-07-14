from __future__ import annotations

import logging
from pathlib import Path

from src.domain.task.models import TaskRecord
from src.domain.task.service import TaskService
from src.infrastructure.workflow.errors import (
    WorkflowRequestFailed,
    WorkflowTimeout,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.worker.executor import WorkerExecutor


class _Repo:
    def __init__(self, claimed: TaskRecord | None):
        self.claimed = claimed
        self.succeeded: tuple[int, dict, str, str] | None = None
        self.failed: tuple[int, str] | None = None

    def upsert_queued(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> TaskRecord:
        return TaskRecord(
            id=1,
            customer_phonenumber=customer_phonenumber,
            status="queued",
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
        )

    def claim_next_queued(self) -> TaskRecord | None:
        item = self.claimed
        self.claimed = None
        return item

    def mark_succeeded(self, *, task_id: int, output: dict, card_id: str, nickname: str) -> None:
        self.succeeded = (task_id, output, card_id, nickname)

    def mark_failed(self, *, task_id: int, error_message: str) -> None:
        self.failed = (task_id, error_message)


class _WorkflowSuccess:
    def __init__(self):
        self.last_args: dict | None = None

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        self.last_args = {
            "customer_phonenumber": customer_phonenumber,
            "work_env": work_env,
            "batch_id": batch_id,
            "task_type": task_type,
        }
        return '{"card_id":"card-1","nickname":"nick-1"}'


class _WorkflowBadJson:
    def run_workflow(self, **_kwargs) -> str:
        return "{bad-json}"


class _WorkflowMissingFields:
    def run_workflow(self, **_kwargs) -> str:
        return '{"nickname":"nick-1"}'


class _WorkflowRequestFail:
    def run_workflow(self, **_kwargs) -> str:
        raise WorkflowRequestFailed("request fail")


class _WorkflowTimeoutFail:
    def run_workflow(self, **_kwargs) -> str:
        raise WorkflowTimeout("timeout")


def _logger() -> logging.Logger:
    logger = logging.getLogger("test_worker_executor")
    logger.handlers = []
    logger.addHandler(logging.StreamHandler())
    logger.setLevel("INFO")
    return logger


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "customer-profile-output.schema.json"


def _parser() -> WorkflowOutputParser:
    return WorkflowOutputParser(schema_path=_schema_path())


def test_executor_success_path():
    repo = _Repo(
        claimed=TaskRecord(
            id=1,
            customer_phonenumber="13800138000",
            status="processing",
            work_env="production",
            batch_id="2026Q2-001",
            task_type="customer_profile",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    workflow = _WorkflowSuccess()
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert repo.succeeded is not None
    assert repo.succeeded[2] == "card-1"
    assert repo.failed is None
    assert workflow.last_args == {
        "customer_phonenumber": "13800138000",
        "work_env": "production",
        "batch_id": "2026Q2-001",
        "task_type": "customer_profile",
    }


def test_executor_passes_invitation_strategy_task_type():
    repo = _Repo(
        claimed=TaskRecord(
            id=10,
            customer_phonenumber="13800138000",
            status="processing",
            task_type="invitation_strategy",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    workflow = _WorkflowSuccess()
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert workflow.last_args is not None
    assert workflow.last_args["task_type"] == "invitation_strategy"


def test_executor_invalid_json_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=2, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowBadJson(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_OUTPUT_INVALID_JSON" in repo.failed[1]


def test_executor_missing_fields_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=3, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowMissingFields(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_OUTPUT_SCHEMA_MISMATCH" in repo.failed[1]


def test_executor_request_failed_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=4, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowRequestFail(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_REQUEST_FAILED" in repo.failed[1]


def test_executor_timeout_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=5, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowTimeoutFail(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_TIMEOUT" in repo.failed[1]

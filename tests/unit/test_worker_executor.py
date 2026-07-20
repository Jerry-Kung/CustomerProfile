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
from src.worker.routing import BurstGate, BurstThresholds


class _Repo:
    def __init__(
        self,
        claimed: TaskRecord | None,
        *,
        queue_depth: int = 0,
        oldest_queued_age_seconds: int | None = None,
    ):
        self.claimed = claimed
        self.succeeded: tuple[int, dict, str | None, str | None] | None = None
        self.failed: tuple[int, str] | None = None
        self.claim_calls = 0
        self.queue_depth = queue_depth
        self.oldest_queued_age_seconds = oldest_queued_age_seconds

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
        self.claim_calls += 1
        item = self.claimed
        self.claimed = None
        return item

    def get_queue_stats(self) -> dict[str, object]:
        return {
            "queue_depth": self.queue_depth,
            "oldest_queued_age_seconds": self.oldest_queued_age_seconds,
        }

    def mark_succeeded(
        self,
        *,
        task_id: int,
        output: dict,
        card_id: str | None,
        nickname: str | None,
    ) -> None:
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


def _burst_gate(*, enabled: bool = True) -> BurstGate:
    return BurstGate(
        BurstThresholds(
            enabled=enabled,
            high_watermark=10,
            low_watermark=3,
            oldest_trigger_seconds=600,
            oldest_recover_seconds=120,
        )
    )


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


def test_executor_missing_profile_fields_marks_succeeded():
    repo = _Repo(claimed=TaskRecord(id=3, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowMissingFields(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert repo.succeeded == (3, {"nickname": "nick-1"}, None, "nick-1")
    assert repo.failed is None


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


def test_local_worker_claims_even_when_burst_gate_is_closed():
    repo = _Repo(
        claimed=TaskRecord(
            id=6,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=0,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="local",
        burst_gate=_burst_gate(enabled=False),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.claim_calls == 1


def test_commercial_worker_does_not_claim_below_burst_threshold():
    repo = _Repo(
        claimed=TaskRecord(
            id=7,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=9,
        oldest_queued_age_seconds=599,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="commercial",
        burst_gate=_burst_gate(),
    )

    assert executor.execute_once() == "idle"
    assert repo.claim_calls == 0


def test_commercial_worker_claims_once_burst_threshold_is_reached():
    repo = _Repo(
        claimed=TaskRecord(
            id=8,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=10,
        oldest_queued_age_seconds=20,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="commercial",
        burst_gate=_burst_gate(),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.claim_calls == 1

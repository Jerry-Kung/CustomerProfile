from __future__ import annotations

import logging
from pathlib import Path

from src.domain.task.models import TaskRecord
from src.domain.task.service import TaskService
from src.config.settings import RetryConfig
from src.infrastructure.qingtian.errors import (
    QingTianRequestFailed,
    QingTianTimeout,
    QingTianWorkflowTerminated,
)
from src.infrastructure.workflow.errors import (
    WorkflowRequestFailed,
    WorkflowTimeout,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.worker.executor import WorkerExecutor
from src.worker.routing import CommercialQueueGate, CommercialQueueThreshold


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
        self.queue_stats_calls = 0
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
        self.queue_stats_calls += 1
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


class _RetryRepo:
    def __init__(self, claimed: TaskRecord):
        self.claimed = claimed
        self.next_attempt_no = claimed.attempt_count + 1
        self.claim_calls = 0
        self.claim_args: list[dict] = []
        self.reserved: list[dict] = []
        self.persisted_runs: list[dict] = []
        self.succeeded_claimed: list[dict] = []
        self.scheduled: list[dict] = []
        self.final_failures: list[dict] = []
        self.stalled_lifetime_checks: list[dict] = []
        self.stall_on_lifetime_check: int | None = None
        self.lease_refreshes = 0
        self.recoveries = 0
        self.legacy_recoveries: list[dict] = []

    def claim_next_eligible(self, **kwargs):
        self.claim_calls += 1
        self.claim_args.append(kwargs)
        item = self.claimed
        self.claimed = None
        return item

    def claim_next_queued(self):
        return None

    def recover_expired_retry_leases(self, *, limit: int):
        self.recoveries += 1
        return 0, 0

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int):
        self.legacy_recoveries.append(
            {"timeout_seconds": timeout_seconds, "limit": limit}
        )
        return 0

    def reserve_workflow_attempt(self, **kwargs):
        self.reserved.append(kwargs)
        return self.next_attempt_no

    def persist_workflow_run(self, **kwargs):
        self.persisted_runs.append(kwargs)
        return True

    def refresh_lease(self, **_kwargs):
        self.lease_refreshes += 1
        return True

    def mark_succeeded_claimed(self, **kwargs):
        self.succeeded_claimed.append(kwargs)
        return True

    def schedule_retry(self, **kwargs):
        self.scheduled.append(kwargs)
        return True

    def mark_failed_claimed(self, **kwargs):
        self.final_failures.append(kwargs)
        return True

    def mark_stalled_if_run_lifetime_exceeded(self, **kwargs):
        self.stalled_lifetime_checks.append(kwargs)
        return self.stall_on_lifetime_check == len(self.stalled_lifetime_checks)


class _RecoverableWorkflow:
    def __init__(self, *, output: str = '{"card_id":"card-1","nickname":"nick-1"}'):
        self.output = output
        self.submit_calls: list[dict] = []
        self.poll_calls: list[dict] = []
        self.poll_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.before_poll_callback = None

    def submit_workflow(self, **kwargs):
        self.submit_calls.append(kwargs)
        if self.submit_error is not None:
            raise self.submit_error
        return "run-new"

    def poll_workflow(self, **kwargs):
        self.poll_calls.append(kwargs)
        if self.before_poll_callback is not None:
            self.before_poll_callback()
        callback = kwargs.get("on_poll")
        if callback:
            callback()
        if self.poll_error is not None:
            raise self.poll_error
        return self.output


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


def _commercial_queue_gate() -> CommercialQueueGate:
    return CommercialQueueGate(
        CommercialQueueThreshold(queue_depth=5)
    )


def _retry_config(
    *,
    max_poll_recoveries: int = 3,
    max_run_lifetime_seconds: int = 7200,
) -> RetryConfig:
    return RetryConfig(
        max_poll_recoveries=max_poll_recoveries,
        max_run_lifetime_seconds=max_run_lifetime_seconds,
        backoff_seconds=(30, 120, 600),
        lease_seconds=180,
        lease_heartbeat_seconds=30,
        recovery_interval_seconds=30,
    )


def _retry_task(
    *,
    workflow_run_id: str | None = None,
    attempt_count: int = 0,
    poll_recovery_count: int = 0,
    execution_backend: str = "local",
    retry_policy_version: str = "local_first_v1",
    workflow_target_id: str | None = None,
    local_attempt_count: int | None = None,
    commercial_attempt_count: int | None = None,
) -> TaskRecord:
    execution_pool = execution_backend
    if local_attempt_count is None:
        local_attempt_count = (
            attempt_count if execution_pool == "local" else min(attempt_count, 2)
        )
    if commercial_attempt_count is None:
        commercial_attempt_count = max(0, attempt_count - local_attempt_count)
    return TaskRecord(
        id=91,
        customer_phonenumber="13800138000",
        status="processing",
        generation=7,
        claim_token="claim-91",
        execution_backend=execution_backend,
        execution_pool=execution_pool,
        workflow_target_id=workflow_target_id or f"{execution_pool}-default",
        workflow_target_revision="v1",
        retry_policy_version=retry_policy_version,
        workflow_run_id=workflow_run_id,
        attempt_count=attempt_count,
        local_attempt_count=local_attempt_count,
        commercial_attempt_count=commercial_attempt_count,
        poll_recovery_count=poll_recovery_count,
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


def test_local_worker_claims_regardless_of_commercial_queue_threshold():
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
        commercial_queue_gate=_commercial_queue_gate(),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.claim_calls == 1


def test_commercial_worker_does_not_claim_normal_task_below_queue_threshold():
    repo = _Repo(
        claimed=TaskRecord(
            id=7,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=4,
        oldest_queued_age_seconds=599,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="commercial",
        commercial_queue_gate=_commercial_queue_gate(),
    )

    assert executor.execute_once() == "idle"
    assert repo.claim_calls == 0


def test_commercial_worker_claims_normal_task_once_queue_threshold_is_reached():
    repo = _Repo(
        claimed=TaskRecord(
            id=8,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=5,
        oldest_queued_age_seconds=20,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="commercial",
        commercial_queue_gate=_commercial_queue_gate(),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.claim_calls == 1


def test_parallel_commercial_worker_skips_queue_pressure_query():
    repo = _Repo(
        claimed=TaskRecord(
            id=9,
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
        worker_role="commercial",
        commercial_queue_gate=CommercialQueueGate(
            CommercialQueueThreshold(
                queue_depth=100,
                routing_mode="parallel",
                spillover_delay_seconds=600,
            )
        ),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.claim_calls == 1
    assert repo.queue_stats_calls == 0


def test_local_only_commercial_worker_skips_queue_pressure_query():
    repo = _Repo(
        claimed=TaskRecord(
            id=10,
            customer_phonenumber="13800138000",
            status="processing",
        ),
        queue_depth=100,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        worker_role="commercial",
        commercial_queue_gate=CommercialQueueGate(
            CommercialQueueThreshold(
                queue_depth=1,
                routing_mode="local_only",
            )
        ),
    )

    assert executor.execute_once() == "idle"
    assert repo.claim_calls == 0
    assert repo.queue_stats_calls == 0


def test_retry_executor_persists_run_id_before_polling_and_guardedly_succeeds():
    repo = _RetryRepo(_retry_task())
    workflow = _RecoverableWorkflow()
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "succeeded"
    assert len(workflow.submit_calls) == 1
    assert workflow.poll_calls[0]["workflow_run_id"] == "run-new"
    assert repo.persisted_runs == [
        {
            "task_id": 91,
            "generation": 7,
            "claim_token": "claim-91",
            "attempt_no": 1,
            "workflow_run_id": "run-new",
        }
    ]
    # The claim already owns a full lease, so the first immediate poll does
    # not perform a redundant same-second renewal.
    assert repo.lease_refreshes == 0
    assert repo.legacy_recoveries == [{"timeout_seconds": 2400, "limit": 100}]
    assert repo.succeeded_claimed[0]["attempt_no"] == 1


def test_shutdown_after_submit_retains_persisted_run_without_counting_recovery():
    repo = _RetryRepo(_retry_task(retry_policy_version="throughput_first_v1"))
    workflow = _RecoverableWorkflow()
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )
    workflow.before_poll_callback = executor.request_shutdown

    assert executor.execute_once() == "retrying"
    assert len(workflow.submit_calls) == 1
    assert len(repo.persisted_runs) == 1
    assert len(repo.scheduled) == 1
    scheduled = repo.scheduled[0]
    assert scheduled["delay_seconds"] == 0
    assert scheduled["retain_workflow_run"] is True
    assert scheduled["terminal_attempt"] is False
    assert scheduled["increment_poll_recovery"] is False
    assert scheduled["next_execution_pool"] is None
    assert scheduled["error_code"] == "WORKER_SHUTDOWN"


def test_lease_heartbeat_waits_one_interval_before_first_renewal(monkeypatch):
    repo = _RetryRepo(_retry_task(workflow_run_id="run-known", attempt_count=1))
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_RecoverableWorkflow(),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )
    monotonic_now = [100.0]
    monkeypatch.setattr(
        "src.worker.executor.time.monotonic",
        lambda: monotonic_now[0],
    )

    heartbeat = executor._lease_heartbeat(
        task_id=91,
        generation=7,
        claim_token="claim-91",
    )
    heartbeat()
    assert repo.lease_refreshes == 0

    monotonic_now[0] = 131.0
    heartbeat()
    assert repo.lease_refreshes == 1


def test_retry_executor_resumes_persisted_run_after_poll_timeout_without_submit():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-known", attempt_count=1))
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianTimeout("poll timeout", stage="poll")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert workflow.submit_calls == []
    assert repo.scheduled[0]["retain_workflow_run"] is True
    assert repo.scheduled[0]["terminal_attempt"] is False
    assert repo.scheduled[0]["increment_poll_recovery"] is True
    assert repo.scheduled[0]["error_stage"] == "poll"


def test_successful_submit_resets_target_failures_even_when_result_poll_times_out():
    repo = _RetryRepo(_retry_task())
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianTimeout("poll timeout", stage="poll")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )
    assert executor._target_circuit.record_failure() is False

    assert executor.execute_once() == "retrying"
    assert executor.operational_snapshot()["target_consecutive_failures"] == 0


def test_retry_executor_does_not_schedule_a_fourth_same_run_poll_recovery():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-known",
            attempt_count=1,
            poll_recovery_count=3,
        )
    )
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianTimeout("poll timeout", stage="poll")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(max_poll_recoveries=3),
    )

    assert executor.execute_once() == "failed"
    assert workflow.submit_calls == []
    assert len(workflow.poll_calls) == 1
    assert repo.scheduled == []
    assert repo.final_failures[0]["error_code"] == "QINGTIAN_RUN_STALLED"


def test_retry_executor_stops_before_poll_when_run_lifetime_has_expired():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-known", attempt_count=1))
    repo.stall_on_lifetime_check = 1
    workflow = _RecoverableWorkflow()
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(max_run_lifetime_seconds=7200),
    )

    assert executor.execute_once() == "failed"
    assert workflow.submit_calls == []
    assert workflow.poll_calls == []
    assert repo.stalled_lifetime_checks == [
        {
            "task_id": 91,
            "generation": 7,
            "claim_token": "claim-91",
            "attempt_no": 1,
            "max_run_lifetime_seconds": 7200,
        }
    ]


def test_retry_executor_stops_after_timeout_when_run_lifetime_expires():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-known", attempt_count=1))
    # First check is immediately before polling; second is immediately before
    # scheduling a recovery, which covers a 40-minute local poll timeout.
    repo.stall_on_lifetime_check = 2
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianTimeout("poll timeout", stage="poll")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(max_run_lifetime_seconds=7200),
    )

    assert executor.execute_once() == "failed"
    assert len(workflow.poll_calls) == 1
    assert repo.scheduled == []
    assert len(repo.stalled_lifetime_checks) == 2


def test_retry_executor_does_not_blindly_resubmit_unknown_submit_timeout():
    repo = _RetryRepo(_retry_task())
    workflow = _RecoverableWorkflow()
    workflow.submit_error = QingTianTimeout("submit timeout", stage="submit")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "failed"
    assert repo.scheduled == []
    assert repo.final_failures[0]["error_code"] == "QINGTIAN_SUBMIT_UNKNOWN"


def test_retry_executor_keeps_explicit_client_rejection_distinct_from_unknown_submit():
    repo = _RetryRepo(_retry_task())
    workflow = _RecoverableWorkflow()
    workflow.submit_error = QingTianRequestFailed(
        "bad workflow input",
        stage="submit",
        status_code=400,
        retryable=False,
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "failed"
    assert repo.scheduled == []
    assert repo.final_failures[0]["error_code"] == "QINGTIAN_REQUEST_FAILED"
    assert executor.operational_snapshot()["target_consecutive_failures"] == 0


def test_retry_executor_retries_known_terminal_workflow_failure_as_new_run():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-known", attempt_count=1))
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated("workflow failed", terminal_status="failed")
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert repo.scheduled[0]["retain_workflow_run"] is False
    assert repo.scheduled[0]["terminal_attempt"] is True
    assert repo.scheduled[0]["error_stage"] == "workflow"


def test_qingtian_durable_retry_is_supported_by_the_commercial_worker():
    repo = _RetryRepo(
        _retry_task(
            execution_backend="commercial",
            attempt_count=2,
        )
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_RecoverableWorkflow(),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="commercial",
        commercial_queue_gate=_commercial_queue_gate(),
        retry_config=_retry_config(),
    )

    assert executor._supports_qingtian_retry() is True
    assert executor.execute_once() == "succeeded"
    assert repo.claim_args == [
        {
            "lease_seconds": 180,
            "execution_pool": "commercial",
            "workflow_target_id": "commercial-default",
            "workflow_target_revision": "v1",
            "allow_new_tasks": False,
            "allow_unbound_retries": True,
            "task_types": ("customer_profile", "invitation_strategy"),
            "work_envs": ("test", "production"),
        }
    ]
    assert repo.reserved[0]["execution_pool"] == "commercial"
    assert repo.reserved[0]["workflow_target_id"] == "commercial-default"
    assert repo.reserved[0]["route_reason"] == "commercial_fallback"


def test_second_definite_local_failure_switches_next_retry_to_commercial():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-local-2", attempt_count=2))
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated(
        "workflow failed",
        terminal_status="failed",
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="local",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert repo.scheduled[0]["next_execution_pool"] == "commercial"
    assert repo.scheduled[0]["retain_workflow_run"] is False
    assert repo.scheduled[0]["terminal_attempt"] is True


def test_first_definite_local_failure_stays_on_local_backend():
    repo = _RetryRepo(_retry_task(workflow_run_id="run-local-1", attempt_count=1))
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated(
        "workflow failed",
        terminal_status="failed",
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="local",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert repo.scheduled[0]["next_execution_pool"] == "local"


def test_single_target_policy_retries_on_the_same_compatible_target():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-invitation-1",
            attempt_count=1,
            retry_policy_version="single_target_v1",
            local_attempt_count=1,
            commercial_attempt_count=0,
        )
    )
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated(
        "invitation workflow failed",
        terminal_status="failed",
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="local",
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert repo.scheduled[0]["next_execution_pool"] == "local"


def test_commercial_fallback_failure_stops_after_one_commercial_attempt():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-commercial-1",
            attempt_count=3,
            execution_backend="commercial",
        )
    )
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated(
        "commercial workflow failed",
        terminal_status="failed",
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="commercial",
        commercial_queue_gate=_commercial_queue_gate(),
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "failed"
    assert repo.scheduled == []
    assert repo.final_failures[0]["error_code"] == "QINGTIAN_RETRY_EXHAUSTED"


def test_throughput_policy_releases_failed_target_back_to_any_pool():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-commercial-01",
            attempt_count=1,
            execution_backend="commercial",
            retry_policy_version="throughput_first_v1",
            workflow_target_id="commercial-01",
            local_attempt_count=0,
            commercial_attempt_count=1,
        )
    )
    workflow = _RecoverableWorkflow()
    workflow.poll_error = QingTianWorkflowTerminated(
        "workflow failed",
        terminal_status="failed",
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="commercial",
        workflow_target_id="commercial-01",
        commercial_queue_gate=_commercial_queue_gate(),
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "retrying"
    assert repo.scheduled[0]["next_execution_pool"] == "any"
    assert repo.scheduled[0]["retain_workflow_run"] is False


def test_throughput_policy_can_reuse_a_target_within_the_shared_total_budget():
    repo = _RetryRepo(
        _retry_task(
            execution_backend="commercial",
            retry_policy_version="throughput_first_v1",
            workflow_target_id="commercial-02",
            local_attempt_count=0,
            commercial_attempt_count=0,
        )
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_RecoverableWorkflow(),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="commercial",
        workflow_target_id="commercial-02",
        commercial_queue_gate=_commercial_queue_gate(),
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "succeeded"
    assert repo.reserved[0]["max_attempts"] == 3
    assert repo.reserved[0]["max_pool_attempts"] == 3
    assert repo.reserved[0]["max_target_attempts"] == 3


def test_worker_never_polls_a_run_owned_by_another_target():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-commercial-01",
            attempt_count=1,
            execution_backend="commercial",
            retry_policy_version="throughput_first_v1",
            workflow_target_id="commercial-01",
            local_attempt_count=0,
            commercial_attempt_count=1,
        )
    )
    workflow = _RecoverableWorkflow()
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="commercial",
        workflow_target_id="commercial-02",
        commercial_queue_gate=_commercial_queue_gate(),
        retry_config=_retry_config(),
    )

    assert executor.execute_once() == "error"
    assert workflow.poll_calls == []


def test_open_target_circuit_blocks_unbound_claims_but_recovers_owned_run():
    repo = _RetryRepo(
        _retry_task(
            workflow_run_id="run-owned",
            attempt_count=1,
        )
    )
    executor = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=_RecoverableWorkflow(),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="qingtian",
        worker_role="local",
        worker_target_failure_threshold=1,
        worker_target_cooldown_seconds=60,
        retry_config=_retry_config(),
    )
    assert executor._target_circuit.record_failure() is True

    assert executor.execute_once() == "succeeded"
    assert repo.claim_args[0]["allow_new_tasks"] is False
    assert repo.claim_args[0]["allow_unbound_retries"] is False

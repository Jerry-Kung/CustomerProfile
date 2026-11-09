from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from src.domain.task.service import TaskService
from src.infrastructure.invitation_runner.client import InvitationRunnerClient
from src.infrastructure.workflow.invitation_writeback import (
    InvitationWritebackUnconfirmed,
)
from src.worker.executor import WorkerExecutor
from test_invitation_runner_client import (
    CONFIG,
    PHONE,
    BATCH,
    EXECUTION,
    RUN_ID,
    acknowledgement,
    detail,
    result,
)
from test_worker_executor import _RetryRepo, _logger, _parser, _retry_task


def executor(*, remote=None, task=None, verifier=None, on_request=None):
    seen = []

    def handle(request):
        seen.append(request)
        if on_request:
            return on_request(request)
        return httpx.Response(
            200,
            json=acknowledgement() if request.method == "POST" else remote or detail(),
        )

    task = task or replace(
        _retry_task(
            retry_policy_version="single_target_v1",
            workflow_target_id="native-invitation",
        ),
        customer_phonenumber=PHONE,
        task_type="invitation_strategy",
        batch_id=BATCH,
        execution_id=EXECUTION,
        work_env="production",
    )
    repo = _RetryRepo(task)
    verifier = verifier or Mock()
    worker = WorkerExecutor(
        task_service=TaskService(repository=repo, logger=_logger()),
        workflow_client=InvitationRunnerClient(CONFIG, httpx.MockTransport(handle)),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="invitation_runner",
        workflow_target_id="native-invitation",
        invitation_writeback=verifier,
    )
    return worker, repo, seen, verifier


def test_execution_identity_crosses_submission_persistence_poll_and_business_verification():
    worker, repo, seen, verifier = executor()
    assert worker.execute_once() == "succeeded"
    assert repo.claim_args[0]["require_invitation_identity"]
    assert repo.persisted_runs[0]["workflow_run_id"] == RUN_ID
    assert repo.succeeded_claimed[0]["output"] == result()
    verifier.verify.assert_called_once_with(
        phone=PHONE, batch_id=BATCH, execution_id=EXECUTION, work_env="production"
    )
    assert [r.method for r in seen] == ["POST", "GET"]
    assert not repo.scheduled and not repo.final_failures


def test_resumed_native_run_never_submits_again():
    task = replace(
        _retry_task(
            workflow_run_id=RUN_ID,
            attempt_count=1,
            retry_policy_version="single_target_v1",
            workflow_target_id="native-invitation",
        ),
        customer_phonenumber=PHONE,
        task_type="invitation_strategy",
        batch_id=BATCH,
        execution_id=EXECUTION,
        work_env="production",
    )
    worker, repo, seen, _ = executor(task=task)
    assert worker.execute_once() == "succeeded"
    assert not repo.reserved and not repo.persisted_runs
    assert [r.method for r in seen] == ["GET"]


def test_model_failure_reaches_final_failure_without_reposting_the_same_terminal_execution():
    worker, repo, seen, verifier = executor(
        remote=detail("failed", delivery="not_attempted", outputs={})
    )
    assert worker.execute_once() == "failed"
    assert not repo.scheduled and len(repo.reserved) == 1
    assert repo.final_failures[0]["error_code"] == "INVITATION_RUNNER_WORKFLOW_FAILED"
    assert repo.final_failures[0]["error_stage"] == "workflow"
    verifier.verify.assert_not_called()


@pytest.mark.parametrize(
    "remote",
    [
        detail("failed", delivery="unknown", outputs={}),
        detail(delivery="skipped"),
        detail(execution_id="newer"),
    ],
)
def test_uncertain_delivery_never_becomes_a_success_or_a_new_run(remote):
    worker, repo, seen, verifier = executor(remote=remote)
    assert worker.execute_once() == "failed"
    assert not repo.scheduled and not repo.succeeded_claimed
    assert repo.final_failures[0]["error_code"].endswith("REQUIRES_RECONCILIATION")
    assert repo.final_failures[0]["error_stage"] == "reconcile"
    verifier.verify.assert_not_called()


def test_business_status_read_failure_retains_the_existing_remote_run():
    verifier = Mock()
    verifier.verify.side_effect = InvitationWritebackUnconfirmed(
        "offline status endpoint"
    )
    worker, repo, seen, _ = executor(verifier=verifier)
    assert worker.execute_once() == "retrying"
    assert (
        repo.scheduled[0]["retain_workflow_run"]
        and repo.scheduled[0]["increment_poll_recovery"]
    )
    assert not repo.final_failures


def test_unconfirmed_submit_does_not_trigger_a_final_failure_callback_or_another_post():
    def handle(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(404)

    worker, repo, seen, verifier = executor(on_request=handle)
    assert worker.execute_once() == "failed"
    assert repo.final_failures[0]["error_code"].endswith("SUBMIT_UNKNOWN")
    assert [r.method for r in seen] == ["POST", "GET"]
    assert not repo.scheduled and not repo.succeeded_claimed

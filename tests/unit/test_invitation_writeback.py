"""A workflow result only completes an identified invitation after API persistence."""

from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from src.config.settings import FailureCallbackConfig
from src.domain.task.service import TaskService
from src.infrastructure.workflow.invitation_writeback import (
    InvitationWritebackMissing, InvitationWritebackSuperseded,
    InvitationWritebackUnconfirmed, InvitationWritebackVerifier,
)
from src.worker.executor import WorkerExecutor
from test_worker_executor import _RetryRepo, _RecoverableWorkflow, _logger, _parser, _retry_task


def status(**changes):
    return {"phone": "13800138000", "batch_id": "audio", "execution_id": "exec-1",
            "analysis_status": 0, "has_script": True, "has_audit": True, **changes}


@pytest.mark.parametrize("body,expected", [
    (status(), None),
    (status(analysis_status=1), InvitationWritebackMissing),
    (status(has_audit=False), InvitationWritebackMissing),
    (status(execution_id="new-execution"), InvitationWritebackSuperseded),
    (status(batch_id="new-batch"), InvitationWritebackSuperseded),
    (status(analysis_status="0"), InvitationWritebackUnconfirmed),
    ({"msg": "old API response"}, InvitationWritebackUnconfirmed),
    ([], InvitationWritebackUnconfirmed),
])
def test_status_response_must_confirm_both_current_result_rows(body, expected):
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    verifier = InvitationWritebackVerifier(
        FailureCallbackConfig(enabled=True, base_url_production="https://api.example.test/",
                              api_key_production="test-key"), transport=httpx.MockTransport(endpoint),
    )
    kwargs = dict(phone="13800138000", batch_id="audio", execution_id="exec-1", work_env="production")
    if expected:
        with pytest.raises(expected):
            verifier.verify(**kwargs)
    else:
        verifier.verify(**kwargs)
    assert requests[0].url.path == "/api/v1/remote/data/invitation/13800138000/status"
    assert requests[0].headers["X-API-Key"] == "test-key"


@pytest.mark.parametrize("error,expected", [
    (None, "succeeded"),
    (InvitationWritebackMissing("no callback"), "failed"),
    (InvitationWritebackSuperseded("new execution"), "failed"),
    (InvitationWritebackUnconfirmed("HTTP 503"), "retrying"),
])
def test_worker_confirms_writeback_and_never_resubmits_a_known_run(error, expected):
    task = replace(_retry_task(workflow_run_id="existing-run", attempt_count=1),
                   task_type="invitation_strategy", execution_id="exec-1", batch_id="audio")
    repo, workflow = _RetryRepo(task), _RecoverableWorkflow(output='{}')
    verifier = Mock()
    verifier.verify.side_effect = error
    executor = WorkerExecutor(task_service=TaskService(repository=repo, logger=_logger()),
                              workflow_client=workflow, output_parser=_parser(), logger=_logger(),
                              platform_name="qingtian", invitation_writeback=verifier)
    assert executor.execute_once() == expected
    assert not workflow.submit_calls and not repo.reserved
    assert len(repo.succeeded_claimed) == int(error is None)
    if isinstance(error, InvitationWritebackUnconfirmed) and not isinstance(error, InvitationWritebackSuperseded):
        assert repo.scheduled[0]["retain_workflow_run"]
        assert repo.scheduled[0]["increment_poll_recovery"]
        assert not repo.final_failures
    elif error:
        assert repo.final_failures[0]["error_code"] == error.code


@pytest.mark.parametrize("http_status", [403, 404, 503])
def test_unavailable_status_endpoint_does_not_prove_execution_failure(http_status):
    verifier = InvitationWritebackVerifier(
        FailureCallbackConfig(enabled=True, base_url_test="https://api.example.test", api_key_test="test-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(http_status)),
    )
    with pytest.raises(InvitationWritebackUnconfirmed):
        verifier.verify(phone="13800138000", batch_id="audio", execution_id="exec-1", work_env="test")

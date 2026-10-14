from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from src.config.settings import ProfileRunnerConfig
from src.infrastructure.runner.client import RunnerClient, WORKFLOW_ID
from src.infrastructure.runner.errors import RunnerRequestFailed, RunnerTimeout, RunnerWorkflowTerminated
from src.infrastructure.workflow.errors import (
    ResumableWorkflowRequestFailed, ResumableWorkflowTimeout, WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch, WorkflowTerminated,
)

PHONE = "13800000000"
BATCH = "batch-business-1"
RUN_ID = "a" * 32
CONFIG = ProfileRunnerConfig(base_url_test="http://test-runner:18000", base_url_production="http://prod-runner:18000",
                              poll_interval_ms=1, poll_timeout_ms=10)


def detail(status="succeeded", **overrides):
    data = {"run_id": RUN_ID, "workflow_id": WORKFLOW_ID, "business_ref": f"cdp:{PHONE}:{BATCH}",
            "is_replay": False, "inputs": {"phone_number": PHONE, "batch_id": BATCH},
            "status": status, "error": "remote workflow error" if status == "failed" else None,
            "outputs": {"result1": json.dumps({"phone": PHONE, "batch_id": BATCH,
                                               "analysis_result": {"profile_summary": {"name": "测试客户"}}})}}
    data.update(overrides)
    return data


def poll(client, **overrides):
    args = {"workflow_run_id": RUN_ID, "customer_phonenumber": PHONE, "batch_id": BATCH,
            "work_env": "production", "task_type": "customer_profile"}
    args.update(overrides)
    return client.poll_workflow(**args)


def submit(client, **overrides):
    args = {"customer_phonenumber": PHONE, "batch_id": BATCH, "work_env": "production", "task_type": "customer_profile"}
    args.update(overrides)
    return client.submit_workflow(**args)


def test_submit_json_maps_fields_and_production_target():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"run_id": RUN_ID, "workflow_id": WORKFLOW_ID})
    client = RunnerClient(CONFIG, httpx.MockTransport(handle))
    assert submit(client) == RUN_ID
    assert len(seen) == 1
    assert str(seen[0].url) == "http://prod-runner:18000/runs"
    assert json.loads(seen[0].content) == {
        "workflow_id": WORKFLOW_ID, "inputs": {"phone_number": PHONE, "batch_id": BATCH},
        "business_ref": f"cdp:{PHONE}:{BATCH}",
    }


@pytest.mark.parametrize("exception, expected, retryable", [
    (httpx.ConnectTimeout, RunnerRequestFailed, True),
    (httpx.PoolTimeout, RunnerRequestFailed, True),
    (httpx.ReadTimeout, RunnerTimeout, False),
    (httpx.WriteTimeout, RunnerTimeout, False),
    (httpx.ConnectError, RunnerRequestFailed, False),
])
def test_submit_network_errors_never_retry_inside_client(exception, expected, retryable):
    calls = []
    def handle(request):
        calls.append(request)
        raise exception("network problem", request=request)
    with pytest.raises(expected) as error:
        submit(RunnerClient(CONFIG, httpx.MockTransport(handle)))
    assert error.value.stage == "submit"
    assert error.value.retryable is retryable
    assert len(calls) == 1


@pytest.mark.parametrize("status, retryable", [(429, True), (401, False), (422, False), (500, False), (503, False), (302, False)])
def test_submit_status_classification_does_not_blindly_retry(status, retryable):
    client = RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(status, headers={"Retry-After": "30"})))
    with pytest.raises(RunnerRequestFailed) as error:
        submit(client)
    assert error.value.status_code == status
    assert error.value.retryable is retryable
    assert error.value.retry_after_seconds == 30


@pytest.mark.parametrize("body", [{}, {"run_id": "../wrong", "workflow_id": WORKFLOW_ID},
                                  {"run_id": RUN_ID, "workflow_id": "wrong"}, {"run_id": None}, [1]])
def test_malformed_submit_response_is_unknown(body):
    client = RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(RunnerRequestFailed) as error:
        submit(client)
    assert error.value.stage == "submit"
    assert error.value.status_code is None
    assert not error.value.retryable


@pytest.mark.parametrize("overrides", [{"task_type": "invitation_strategy"}, {"batch_id": None},
                                      {"customer_phonenumber": "bad-phone"}, {"work_env": "develop"}])
def test_invalid_submit_inputs_never_touch_transport(overrides):
    def handle(_):
        pytest.fail("must not submit invalid input")
    with pytest.raises(RunnerRequestFailed):
        submit(RunnerClient(CONFIG, httpx.MockTransport(handle)), **overrides)


def test_missing_test_url_does_not_use_production_url():
    def handle(_):
        pytest.fail("must not fall back to production")
    with pytest.raises(RunnerRequestFailed, match="test"):
        submit(RunnerClient(replace(CONFIG, base_url_test=""), httpx.MockTransport(handle)), work_env="test")


def test_poll_returns_inner_analysis_result_and_renews_lease():
    statuses = iter(["queued", "running", "succeeded"])
    requests_seen = []
    def handle(request):
        requests_seen.append(request)
        return httpx.Response(200, json=detail(next(statuses)))
    heartbeats = []
    result = poll(RunnerClient(CONFIG, httpx.MockTransport(handle)), on_poll=lambda: heartbeats.append(True))
    assert json.loads(result) == {"profile_summary": {"name": "测试客户"}}
    assert len(heartbeats) >= 3
    assert all(request.method == "GET" and request.url.path == "/runs/" + RUN_ID for request in requests_seen)


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_failed_run_raises_platform_terminal_error(status):
    with pytest.raises(RunnerWorkflowTerminated) as error:
        poll(RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=detail(status)))))
    assert error.value.terminal_status == status
    assert error.value.stage == "workflow"
    assert isinstance(error.value, WorkflowTerminated)


@pytest.mark.parametrize("overrides", [
    {"is_replay": True}, {"run_id": "b" * 32}, {"workflow_id": "wrong"}, {"business_ref": "wrong"},
    {"inputs": {"phone_number": "13900000001", "batch_id": BATCH}},
    {"inputs": {"phone_number": PHONE, "batch_id": "wrong"}},
])
def test_poll_rejects_wrong_run_identity(overrides):
    with pytest.raises(RunnerRequestFailed, match="identity") as error:
        poll(RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=detail(**overrides)))))
    assert error.value.stage == "poll"


def test_bad_result_json_and_wrong_business_result_are_distinct():
    with pytest.raises(WorkflowOutputInvalidJson):
        poll(RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=detail(outputs={"result1": "bad"})))))
    output = {"phone": "13900000001", "batch_id": BATCH, "analysis_result": {}}
    with pytest.raises(WorkflowOutputSchemaMismatch):
        poll(RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=detail(outputs={"result1": json.dumps(output)})))))


def test_poll_timeout_preserves_remote_run_and_never_posts():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=detail("running"))
    with pytest.raises(RunnerTimeout) as error:
        poll(RunnerClient(CONFIG, httpx.MockTransport(handle)))
    assert error.value.stage == "poll"
    assert all(request.method == "GET" for request in seen)
    assert isinstance(error.value, ResumableWorkflowTimeout)


def test_get_network_error_is_recoverable_and_generic_errors_remain_compatible():
    def handle(request):
        raise httpx.ConnectError("offline", request=request)
    with pytest.raises(RunnerRequestFailed) as error:
        poll(RunnerClient(CONFIG, httpx.MockTransport(handle)))
    assert error.value.stage == "poll" and error.value.retryable
    assert isinstance(error.value, ResumableWorkflowRequestFailed)
    from src.infrastructure.qingtian.errors import QingTianRequestFailed, QingTianTimeout, QingTianWorkflowTerminated
    qt = QingTianRequestFailed("x", stage="submit", status_code=429, retryable=True)
    assert qt.stage == "submit" and qt.status_code == 429 and qt.retryable
    assert QingTianTimeout("x", stage="poll").stage == "poll"
    terminated = QingTianWorkflowTerminated("x", terminal_status="failed")
    assert isinstance(terminated, QingTianRequestFailed) and isinstance(terminated, WorkflowTerminated)
    assert terminated.terminal_status == "failed" and terminated.retryable


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_stopped_run_requires_reconciliation(status):
    with pytest.raises(RunnerRequestFailed) as error:
        poll(RunnerClient(CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=detail(status)))))
    assert error.value.stage == "reconcile"

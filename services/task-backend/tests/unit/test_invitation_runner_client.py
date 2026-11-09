from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from src.config.settings import InvitationRunnerConfig
from src.infrastructure.invitation_runner.client import (
    InvitationRunnerClient,
    TASK_PATH,
    WORKFLOW_ID,
    invitation_run_id,
)
from src.infrastructure.invitation_runner.errors import (
    InvitationRunnerRequestFailed,
    InvitationRunnerTimeout,
    InvitationRunnerWorkflowTerminated,
)

PHONE = "13800000000"
BATCH = "call-001"
EXECUTION = "execution-001"
RUN_ID = invitation_run_id(PHONE, BATCH, EXECUTION)
BODY = {
    "phone_number": PHONE,
    "batch_id": BATCH,
    "execution_id": EXECUTION,
    "work_env": "production",
}
CONFIG = InvitationRunnerConfig(
    base_url_test="http://invitation-test:8099",
    base_url_production="http://invitation-prod:8099",
    api_key_test="offline-test-key",
    api_key_production="offline-production-key",
    poll_interval_ms=1,
    poll_timeout_ms=50,
)


def result(**changes):
    return {
        "phone": PHONE,
        "batch_id": BATCH,
        "execution_id": EXECUTION,
        "generated_at": "2026-10-29T10:30:00+08:00",
        "script": {"strategy": {}},
        "audit": {"dimensions": []},
        "follow_up_items": [],
        "follow_up_summary": "无有效通话内容",
        **changes,
    }


def acknowledgement(**changes):
    return {
        "status": "accepted",
        "workflow_id": WORKFLOW_ID,
        "run_id": RUN_ID,
        **BODY,
        **changes,
    }


def detail(status="succeeded", **changes):
    return {
        "run_id": RUN_ID,
        "workflow_id": WORKFLOW_ID,
        **BODY,
        "status": status,
        "delivery": "confirmed" if status == "succeeded" else "pending",
        "outputs": {
            "result": json.dumps(result(), ensure_ascii=False),
            "delivery": "confirmed",
        },
        **changes,
    }


def submit(client, **changes):
    return client.submit_workflow(
        customer_phonenumber=PHONE,
        batch_id=BATCH,
        execution_id=EXECUTION,
        work_env=changes.pop("work_env", "production"),
        **changes,
    )


def poll(client, **changes):
    args = {
        "workflow_run_id": RUN_ID,
        "customer_phonenumber": PHONE,
        "batch_id": BATCH,
        "execution_id": EXECUTION,
        "work_env": "production",
    }
    args.update(changes)
    return client.poll_workflow(**args)


@pytest.mark.parametrize(
    "env, host, key",
    [
        ("test", "invitation-test", "offline-test-key"),
        ("production", "invitation-prod", "offline-production-key"),
    ],
)
def test_submit_matches_native_authenticated_contract(env, host, key):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=acknowledgement(work_env=env))

    assert (
        submit(
            InvitationRunnerClient(CONFIG, httpx.MockTransport(handle)), work_env=env
        )
        == RUN_ID
    )
    assert len(seen) == 1
    assert seen[0].url.host == host and seen[0].url.path == TASK_PATH
    assert seen[0].headers["X-API-Key"] == key
    assert json.loads(seen[0].content) == {**BODY, "work_env": env}
    assert len(RUN_ID) == 68


@pytest.mark.parametrize(
    "field,value",
    [
        ("customer_phonenumber", "bad-phone"),
        ("batch_id", None),
        ("batch_id", " "),
        ("batch_id", "x" * 51),
        ("execution_id", None),
        ("execution_id", "bad/id"),
        ("task_type", "customer_profile"),
        ("work_env", "prod"),
    ],
)
def test_invalid_input_never_reaches_network(field, value):
    args = {
        "customer_phonenumber": PHONE,
        "batch_id": BATCH,
        "execution_id": EXECUTION,
        "work_env": "production",
    }
    args[field] = value
    transport = httpx.MockTransport(
        lambda _: pytest.fail("invalid input reached network")
    )
    with pytest.raises(InvitationRunnerRequestFailed):
        InvitationRunnerClient(CONFIG, transport).submit_workflow(**args)


def test_missing_environment_url_or_key_never_falls_back():
    transport = httpx.MockTransport(
        lambda _: pytest.fail("unexpected environment fallback")
    )
    for config in [replace(CONFIG, base_url_test=""), replace(CONFIG, api_key_test="")]:
        with pytest.raises(InvitationRunnerRequestFailed, match="test"):
            submit(InvitationRunnerClient(config, transport), work_env="test")


def test_poll_uses_exact_identity_and_returns_the_whole_invitation_object():
    states = iter([detail("queued"), detail("running"), detail()])
    seen, leases = [], []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=next(states))

    output = poll(
        InvitationRunnerClient(CONFIG, httpx.MockTransport(handle)),
        on_poll=lambda: leases.append(True),
    )
    assert json.loads(output) == result()
    assert len(leases) >= 3
    assert all(
        r.method == "GET" and r.url.path == TASK_PATH + "/" + RUN_ID for r in seen
    )
    assert all(r.headers["X-API-Key"] == CONFIG.api_key_production for r in seen)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "wrong"},
        {"workflow_id": "customer_profile_pipeline"},
        {"phone_number": "13900000000"},
        {"batch_id": "wrong"},
        {"execution_id": "new-execution"},
        {"work_env": "test"},
        {"is_replay": True},
        {"is_replay": 1},
    ],
)
def test_poll_rejects_mismatched_remote_identity(changes):
    client = InvitationRunnerClient(
        CONFIG,
        httpx.MockTransport(lambda _: httpx.Response(200, json=detail(**changes))),
    )
    with pytest.raises(InvitationRunnerRequestFailed) as error:
        poll(client)
    assert error.value.stage == "reconcile"


@pytest.mark.parametrize(
    "changes",
    [
        {"phone": "13900000000"},
        {"execution_id": "wrong"},
        {"batch_id": "wrong"},
        {"script": {}},
        {"audit": {}},
        {"follow_up_items": None},
    ],
)
def test_result_envelope_must_match_execution(changes):
    data = detail(outputs={"result": json.dumps(result(**changes))})
    with pytest.raises(InvitationRunnerRequestFailed) as error:
        poll(
            InvitationRunnerClient(
                CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=data))
            )
        )
    assert error.value.stage == "reconcile"


@pytest.mark.parametrize("delivery", ["skipped", "unknown", None])
def test_runtime_success_without_confirmed_delivery_is_not_business_success(delivery):
    client = InvitationRunnerClient(
        CONFIG,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=detail(delivery=delivery))
        ),
    )
    with pytest.raises(InvitationRunnerRequestFailed) as error:
        poll(client)
    assert error.value.stage == "reconcile"


def test_known_terminal_failure_is_not_a_new_run_retry():
    data = detail("failed", delivery="not_attempted", outputs={})
    with pytest.raises(InvitationRunnerWorkflowTerminated) as error:
        poll(
            InvitationRunnerClient(
                CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=data))
            )
        )
    assert error.value.stage == "workflow" and not error.value.retryable


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
def test_uncertain_failure_requires_reconciliation(status):
    data = detail(status, delivery="unknown", outputs={})
    with pytest.raises(InvitationRunnerRequestFailed) as error:
        poll(
            InvitationRunnerClient(
                CONFIG, httpx.MockTransport(lambda _: httpx.Response(200, json=data))
            )
        )
    assert error.value.stage == "reconcile" and not error.value.retryable


@pytest.mark.parametrize("recoverable", [True, False])
def test_lost_acknowledgement_is_recovered_with_one_read_and_never_another_post(
    recoverable,
):
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("offline", request=request)
        return (
            httpx.Response(200, json=detail("queued"))
            if recoverable
            else httpx.Response(404)
        )

    client = InvitationRunnerClient(CONFIG, httpx.MockTransport(handle))
    if recoverable:
        assert submit(client) == RUN_ID
    else:
        with pytest.raises(InvitationRunnerTimeout) as error:
            submit(client)
        assert error.value.stage == "submit"
    assert [r.method for r in seen] == ["POST", "GET"]
    assert seen[1].url.path.endswith(RUN_ID)


@pytest.mark.parametrize(
    "status,stage,retryable",
    [
        (429, "submit", True),
        (401, "submit", False),
        (422, "submit", False),
        (409, "reconcile", False),
    ],
)
def test_explicit_rejections_preserve_retry_semantics(status, stage, retryable):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, headers={"Retry-After": "30"})

    with pytest.raises(InvitationRunnerRequestFailed) as error:
        submit(InvitationRunnerClient(CONFIG, httpx.MockTransport(handle)))
    assert error.value.stage == stage and error.value.retryable is retryable
    assert error.value.retry_after_seconds == 30 and len(seen) == 1


@pytest.mark.parametrize("response", [{}, {"run_id": "../wrong"}, [1], None])
def test_bad_acknowledgement_is_unknown_when_lookup_cannot_prove_admission(response):
    def handle(request):
        return (
            httpx.Response(200, json=response)
            if request.method == "POST"
            else httpx.Response(404)
        )

    with pytest.raises(InvitationRunnerRequestFailed) as error:
        submit(InvitationRunnerClient(CONFIG, httpx.MockTransport(handle)))
    assert error.value.stage == "submit" and not error.value.retryable


def test_poll_timeout_never_resubmits_a_known_run():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=detail("running"))

    with pytest.raises(InvitationRunnerTimeout) as error:
        poll(
            InvitationRunnerClient(
                replace(CONFIG, poll_timeout_ms=3), httpx.MockTransport(handle)
            )
        )
    assert error.value.stage == "poll" and all(r.method == "GET" for r in seen)


def test_run_id_changes_for_a_new_execution_and_preserves_unicode_batch_identity():
    assert invitation_run_id(PHONE, BATCH, "another") != RUN_ID
    import hashlib

    assert (
        invitation_run_id(PHONE, "录音-一", EXECUTION)
        == "inv_"
        + hashlib.sha256(
            json.dumps([PHONE, "录音-一", EXECUTION], ensure_ascii=False).encode()
        ).hexdigest()
    )

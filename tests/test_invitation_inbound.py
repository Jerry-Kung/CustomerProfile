"""邀约接单必须带执行标识，并且重复投递不会重置在飞或已完成任务。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from customer_profile.inbound import create_inbound_app
from customer_profile.invitation_inbound import INVITATION_TASK_PATH as PATH
from customer_profile.runner import build_service
from customer_profile.settings import Settings
from customer_profile.worker import Worker
from customer_profile.workflows.invitation import WORKFLOW_ID

from .test_invitation import OfflineServices

KEY = "offline-inbound-key"
HEADERS = {"X-API-Key": KEY}
PAYLOAD = {
    "phone_number": "13800000000",
    "batch_id": "call-001",
    "execution_id": "execution-001",
    "work_env": "test",
}


@pytest.fixture
async def inbound_client(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'inbound.db'}",
        data_dir=tmp_path,
        inbound_api_key=KEY,
        inline_worker=False,
        interrupt_on_start=False,
        replay_mode="off",
        llm_base_url="https://llm.invalid/v1",
        llm_api_key="offline-key",
        llm_model="offline",
        profile_api_base_url="https://m2m.invalid",
        profile_api_key="offline-key",
        http_retry_enabled=False,
    )

    async def factory():
        return await build_service(settings)

    app = create_inbound_app(service_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://inbound"
        ) as client:
            yield client, app.state.service


async def test_submit_is_persisted_and_query_is_authenticated(inbound_client):
    client, service = inbound_client
    response = await client.post(PATH, json=PAYLOAD, headers=HEADERS)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["execution_id"] == PAYLOAD["execution_id"]
    assert accepted["workflow_id"] == WORKFLOW_ID
    row = await service.store.get_run(accepted["run_id"])
    assert row["status"] == "queued" and row["inputs_json"] == PAYLOAD
    query = PATH + "/" + accepted["run_id"]
    assert (await client.get(query)).status_code == 401
    state = (await client.get(query, headers=HEADERS)).json()
    assert (
        state["status"] == "queued" and state["execution_id"] == PAYLOAD["execution_id"]
    )
    assert state["outputs"] == {}


@pytest.mark.parametrize(
    "status", ["queued", "running", "succeeded", "failed", "interrupted"]
)
async def test_duplicate_does_not_requeue_any_existing_state(inbound_client, status):
    client, service = inbound_client
    first = (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).json()
    await service.store.set_run_status(first["run_id"], status)
    duplicate = await client.post(PATH, json=PAYLOAD, headers=HEADERS)
    assert duplicate.status_code == 200
    assert duplicate.json()["run_id"] == first["run_id"]
    assert (await service.store.get_run(first["run_id"]))["status"] == status
    assert await service.store.count_runs(workflow_id=WORKFLOW_ID) == 1


async def test_simultaneous_duplicates_share_one_queue_row(inbound_client):
    client, service = inbound_client
    service.settings.max_queued_runs = 1
    responses = await asyncio.gather(
        *(client.post(PATH, json=PAYLOAD, headers=HEADERS) for _ in range(8))
    )
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert await service.store.count_runs(workflow_id=WORKFLOW_ID) == 1


async def test_environment_cannot_change_under_the_same_execution(inbound_client):
    client, service = inbound_client
    await client.post(PATH, json=PAYLOAD, headers=HEADERS)
    response = await client.post(
        PATH, json={**PAYLOAD, "work_env": "production"}, headers=HEADERS
    )
    assert response.status_code == 409
    assert await service.store.count_runs(workflow_id=WORKFLOW_ID) == 1


async def test_new_execution_creates_a_new_task(inbound_client):
    client, service = inbound_client
    first = (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).json()
    second = (
        await client.post(
            PATH, json={**PAYLOAD, "execution_id": "execution-002"}, headers=HEADERS
        )
    ).json()
    assert first["run_id"] != second["run_id"]
    assert await service.store.count_runs(workflow_id=WORKFLOW_ID) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_id": None},
        {"execution_id": "bad/identity"},
        {"phone_number": "../bad"},
        {"work_env": "prod"},
        {"batch_id": "  "},
        {"batch_id": "x" * 51},
    ],
)
async def test_invalid_input_never_enters_queue(inbound_client, overrides):
    client, service = inbound_client
    response = await client.post(PATH, json={**PAYLOAD, **overrides}, headers=HEADERS)
    assert response.status_code == 422
    assert await service.store.queue_depth() == 0


async def test_missing_execution_id_is_rejected(inbound_client):
    client, service = inbound_client
    response = await client.post(
        PATH,
        json={k: v for k, v in PAYLOAD.items() if k != "execution_id"},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert await service.store.queue_depth() == 0


async def test_auth_and_queue_capacity(inbound_client):
    client, service = inbound_client
    assert (await client.post(PATH, json=PAYLOAD)).status_code == 401
    assert (
        await client.post(PATH, json=PAYLOAD, headers={"X-API-Key": "wrong"})
    ).status_code == 403
    service.settings.inbound_api_key = ""
    assert (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).status_code == 503
    service.settings.inbound_api_key = KEY
    service.settings.max_queued_runs = 1
    first = await client.post(PATH, json=PAYLOAD, headers=HEADERS)
    assert first.status_code == 200
    assert (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).status_code == 200
    assert (
        await client.post(
            PATH, json={**PAYLOAD, "execution_id": "another"}, headers=HEADERS
        )
    ).status_code == 429


async def test_invitation_query_does_not_expose_profile_runs(inbound_client):
    client, service = inbound_client
    run_id = await service.submit(
        "customer_profile_pipeline", {"phone_number": PAYLOAD["phone_number"]}
    )
    response = await client.get(PATH + "/" + run_id, headers=HEADERS)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "mode, expected_status, delivery",
    [
        ("normal", "succeeded", "confirmed"),
        ("shadow", "succeeded", "skipped"),
        ("bad_model", "failed", "not_attempted"),
        ("stale", "failed", "unknown"),
        ("callback_timeout", "failed", "unknown"),
    ],
)
async def test_queue_worker_and_public_status_agree(
    inbound_client, mode, expected_status, delivery
):
    client, service = inbound_client
    services = OfflineServices(
        [],
        bad_script=3 if mode == "bad_model" else 0,
        readback_execution="newer-execution"
        if mode == "stale"
        else PAYLOAD["execution_id"],
        callback_timeout=mode == "callback_timeout",
    )
    transport = httpx.MockTransport(services.handle)
    service.llm_client._transport = transport
    service.llm_client._backoff_seconds = lambda _: 0
    service.http_client._transport = transport
    service.settings.writeback_enabled = mode != "shadow"
    accepted = (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).json()
    assert await Worker(service, service.settings).run_once()
    state = (await client.get(PATH + "/" + accepted["run_id"], headers=HEADERS)).json()
    assert state["status"] == expected_status
    assert state["delivery"] == delivery
    assert state["execution_id"] == PAYLOAD["execution_id"]
    assert (await client.post(PATH, json=PAYLOAD, headers=HEADERS)).json()[
        "run_id"
    ] == accepted["run_id"]
    assert await service.store.queue_depth() == 0

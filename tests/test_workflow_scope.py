"""两个独立执行进程共享历史库时，领取、恢复、容量与接单必须按任务归属隔离。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from customer_profile.api import ENTRY_WORKFLOW_ID, create_app
from customer_profile.inbound import create_inbound_app
from customer_profile.persistence import Store
from customer_profile.persistence.schema import RunRecordRow
from customer_profile.replay import ReplaySource
from customer_profile.runner import QueueFull, build_service
from customer_profile.worker import Worker
from customer_profile.workflows.invitation import WORKFLOW_ID as INVITATION

from .conftest import make_settings


async def add_run(store, run_id, workflow_id, *, status="queued", parent=None):
    await store.enqueue_run(RunRecordRow(run_id=run_id, workflow_id=workflow_id))
    await store._write(
        "UPDATE workflow_runs SET status = ?, parent_run_id = ? WHERE run_id = ?",
        (status, parent, run_id),
    )


async def scoped_service(tmp_path, scope, *, recover=False, **overrides):
    settings = make_settings(
        tmp_path,
        _env_file=None,
        workflow_scope=scope,
        inline_worker=False,
        interrupt_on_start=recover,
        inbound_api_key="offline-scope-key",
        worker_concurrency=None,
        worker_poll_interval_ms=10,
        **overrides,
    )
    return await build_service(settings, replay=ReplaySource(strict=True))


async def close(service):
    await service.aclose()
    service.store.close()


async def test_scoped_stores_claim_only_their_own_roots(tmp_path):
    first, second = Store(tmp_path / "shared.db"), Store(tmp_path / "shared.db")
    first.connect()
    second.connect()
    try:
        await add_run(first, "profile", ENTRY_WORKFLOW_ID)
        await add_run(first, "invitation", INVITATION)
        await add_run(
            first, "child", "invitation_audio_content_extract", parent="profile"
        )
        assert await first.queue_depth(workflow_scope="profile") == 1
        assert await second.queue_depth(workflow_scope="invitation") == 1
        profile, invitation = await asyncio.gather(
            first.claim_next_queued_run(
                "profile-worker", lease_ms=60_000, workflow_scope="profile"
            ),
            second.claim_next_queued_run(
                "invitation-worker", lease_ms=60_000, workflow_scope="invitation"
            ),
        )
        assert profile["run_id"] == "profile"
        assert invitation["run_id"] == "invitation"
        assert (
            await first.claim_next_queued_run(
                "profile-worker", lease_ms=60_000, workflow_scope="profile"
            )
            is None
        )
        assert (
            await second.claim_next_queued_run(
                "invitation-worker", lease_ms=60_000, workflow_scope="invitation"
            )
            is None
        )
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("scope", ["profile", "invitation"])
async def test_recovery_follows_ancestry_and_never_marks_the_other_scope(
    tmp_path, scope
):
    store = Store(tmp_path / "recovery.db")
    store.connect()
    try:
        for owner, workflow_id in (
            ("profile", ENTRY_WORKFLOW_ID),
            ("invitation", INVITATION),
        ):
            await add_run(store, owner, workflow_id, status="running")
            # 故意使用相同子工作流名称，且名称看似属于另一类；恢复必须沿主任务归属判断。
            await add_run(
                store,
                owner + "-child",
                "invitation_shared_audio",
                status="running",
                parent=owner,
            )
            await add_run(
                store,
                owner + "-grandchild",
                "shared_parser",
                status="running",
                parent=owner + "-child",
            )
            await add_run(store, owner + "-queued", workflow_id)
        assert await store.mark_running_as_interrupted(workflow_scope=scope) == 3
        other = "profile" if scope == "invitation" else "invitation"
        for suffix in ("", "-child", "-grandchild"):
            assert (await store.get_run(scope + suffix))["status"] == "interrupted"
            assert (await store.get_run(other + suffix))["status"] == "running"
        for owner in (scope, other):
            assert (await store.get_run(owner + "-queued"))["status"] == "queued"
    finally:
        store.close()


@pytest.mark.parametrize("running_scope", ["profile", "invitation"])
async def test_starting_and_stopping_other_worker_keeps_live_task_running(
    tmp_path, monkeypatch, running_scope
):
    active = await scoped_service(tmp_path, running_scope)
    active_worker = Worker(active, active.settings)
    started, release = asyncio.Event(), asyncio.Event()
    workflow_id = INVITATION if running_scope == "invitation" else ENTRY_WORKFLOW_ID
    run_id = await active.submit(workflow_id, {})

    async def held_execute(rid, *_):
        started.set()
        await release.wait()
        await active.store.set_run_status(rid, "succeeded")

    monkeypatch.setattr(active_worker, "_execute", held_execute)
    active_loop = asyncio.create_task(active_worker.run_forever())
    other = None
    other_loop = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        other_scope = "invitation" if running_scope == "profile" else "profile"
        # build_service 的启动恢复和 worker 的启动恢复都不能影响另一组的在飞任务。
        other = await scoped_service(tmp_path, other_scope, recover=True)
        other_worker = Worker(other, other.settings)
        recovered = asyncio.Event()
        recover = other.store.mark_running_as_interrupted

        async def observe_recovery(**kwargs):
            result = await recover(**kwargs)
            recovered.set()
            return result

        monkeypatch.setattr(
            other.store, "mark_running_as_interrupted", observe_recovery
        )
        other_loop = asyncio.create_task(other_worker.run_forever())
        await asyncio.wait_for(recovered.wait(), timeout=5)
        assert (await active.store.get_run(run_id))["status"] == "running"
        other_worker.request_stop()
        await asyncio.wait_for(other_loop, timeout=5)
        assert (await active.store.get_run(run_id))["status"] == "running"
        active_worker.request_stop()
        release.set()
        await asyncio.wait_for(active_loop, timeout=5)
        assert (await active.store.get_run(run_id))["status"] == "succeeded"
    finally:
        active_worker.request_stop()
        release.set()
        await asyncio.gather(active_loop, return_exceptions=True)
        if other is not None:
            other_worker.request_stop()
            if other_loop is not None:
                await asyncio.gather(other_loop, return_exceptions=True)
            await close(other)
        await close(active)


async def test_scope_has_its_own_queue_capacity_and_rejects_wrong_root(tmp_path):
    profile = await scoped_service(tmp_path, "profile", max_queued_runs=1)
    invitation = await scoped_service(tmp_path, "invitation", max_queued_runs=1)
    try:
        await profile.submit(ENTRY_WORKFLOW_ID, {})
        await invitation.submit(INVITATION, {})
        assert await profile.store.queue_depth() == 2
        assert (await profile.store.queue_snapshot(workflow_scope="profile"))[
            "queued"
        ] == 1
        assert (await invitation.store.queue_snapshot(workflow_scope="invitation"))[
            "queued"
        ] == 1
        with pytest.raises(QueueFull):
            await invitation.submit(INVITATION, {})
        with pytest.raises(KeyError, match="不接收"):
            await profile.submit(INVITATION, {})
        with pytest.raises(KeyError, match="不接收"):
            await invitation.submit(ENTRY_WORKFLOW_ID, {})
    finally:
        await close(profile)
        await close(invitation)


@pytest.mark.parametrize("scope", ["profile", "invitation"])
async def test_http_admission_keeps_existing_protocols_and_rejects_other_task_type(
    tmp_path, scope
):
    service = await scoped_service(tmp_path, scope)

    async def factory():
        return service

    app = (
        create_app(service_factory=factory)
        if scope == "profile"
        else create_inbound_app(service_factory=factory)
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://offline"
        ) as client:
            invitation = {
                "phone_number": "13800000000",
                "batch_id": "b",
                "execution_id": "e",
                "work_env": "test",
            }
            if scope == "profile":
                payload = {
                    "workflow_id": ENTRY_WORKFLOW_ID,
                    "inputs": {"phone_number": "13800000000", "batch_id": "b"},
                }
                assert (await client.post("/runs", json=payload)).status_code == 200
                payload.update(workflow_id=INVITATION, inputs=invitation)
                assert (await client.post("/runs", json=payload)).status_code == 404
                assert all(
                    not row["workflow_id"].startswith("invitation_")
                    for row in (await client.get("/workflows")).json()
                )
            else:
                headers = {"X-API-Key": "offline-scope-key"}
                accepted = await client.post(
                    "/api/v1/invitation-tasks", json=invitation, headers=headers
                )
                assert accepted.status_code == 200
                query = await client.get(
                    "/api/v1/invitation-tasks/" + accepted.json()["run_id"],
                    headers=headers,
                )
                assert query.status_code == 200
                assert query.json()["execution_id"] == "e"
                assert (
                    await client.post("/api/v1/tasks", json=invitation, headers=headers)
                ).status_code == 404

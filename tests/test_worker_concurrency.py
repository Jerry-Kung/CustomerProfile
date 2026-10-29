"""真实 SQLite 队列上的主任务并发、唯一领取与优雅停机。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from customer_profile.definitions import RunStatus
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.settings import Settings
from customer_profile.worker import Worker
from customer_profile.workflows import mengshi_it_system_data as workflow

from .conftest import make_settings


async def make_worker(tmp_path, concurrency, **overrides):
    settings = make_settings(
        tmp_path,
        _env_file=None,
        inline_worker=False,
        interrupt_on_start=False,
        worker_concurrency=concurrency,
        worker_poll_interval_ms=10,
        **overrides,
    )
    service = await build_service(settings, replay=ReplaySource(strict=True))
    return Worker(service, settings)


@pytest.mark.parametrize("concurrency", [1, 3, 10])
async def test_queue_slots_bound_parallel_runs_and_stop_without_claiming_more(
    tmp_path, monkeypatch, concurrency
):
    worker = await make_worker(tmp_path, concurrency)
    service = worker.service
    started = asyncio.Queue()
    release = asyncio.Semaphore(0)
    execute = worker._execute
    active = 0
    peak = 0

    async def held_execute(run_id, workflow_id, inputs, business_ref):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.put_nowait(run_id)
        try:
            await release.acquire()
            await execute(run_id, workflow_id, inputs, business_ref)
        finally:
            active -= 1

    monkeypatch.setattr(worker, "_execute", held_execute)
    run_ids = [
        await service.submit(workflow.WORKFLOW_ID, workflow.default_inputs("[]"))
        for _ in range(concurrency + 2)
    ]
    loop = asyncio.create_task(worker.run_forever())
    try:
        claimed = [
            await asyncio.wait_for(started.get(), timeout=5) for _ in range(concurrency)
        ]
        rows = [await service.store.get_run(run_id) for run_id in run_ids]
        assert sum(row["status"] == RunStatus.RUNNING for row in rows) == concurrency
        assert sum(row["status"] == RunStatus.QUEUED for row in rows) == 2
        assert len(set(claimed)) == concurrency

        # 空出一个槽位后才能领下一条；上限不受 submit_local 的缺省 4 影响。
        release.release()
        claimed.append(await asyncio.wait_for(started.get(), timeout=5))
        assert peak == concurrency
        assert len(set(claimed)) == concurrency + 1

        worker.request_stop()
        assert not loop.done(), "停机应等待已领取的任务完成"
        for _ in range(concurrency):
            release.release()
        await asyncio.wait_for(loop, timeout=5)
        rows = [await service.store.get_run(run_id) for run_id in run_ids]
        assert (
            sum(row["status"] == RunStatus.SUCCEEDED for row in rows) == concurrency + 1
        )
        assert sum(row["status"] == RunStatus.QUEUED for row in rows) == 1
        assert active == 0
        assert started.empty(), "停机后不能继续领取排队任务"
    finally:
        worker.request_stop()
        for _ in run_ids:
            release.release()
        await asyncio.wait_for(loop, timeout=5)
        await worker.aclose()


@pytest.mark.parametrize("concurrency", [2, None])
async def test_claim_failure_stops_new_claims_but_drains_another_slot(
    tmp_path, monkeypatch, concurrency
):
    worker = await make_worker(tmp_path, concurrency)
    service = worker.service
    run_ids = [
        await service.submit(workflow.WORKFLOW_ID, workflow.default_inputs("[]"))
        for _ in range(3)
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    stopping = asyncio.Event()
    execute = worker._execute
    claim = service.store.claim_next_queued_run
    request_stop = worker.request_stop
    claim_count = 0

    async def failing_claim(*args, **kwargs):
        nonlocal claim_count
        claim_count += 1
        if claim_count == 2:
            await started.wait()
            raise RuntimeError("isolated database claim failure")
        return await claim(*args, **kwargs)

    async def held_execute(*args):
        started.set()
        await release.wait()
        await execute(*args)

    def stop():
        request_stop()
        stopping.set()

    monkeypatch.setattr(service.store, "claim_next_queued_run", failing_claim)
    monkeypatch.setattr(worker, "_execute", held_execute)
    monkeypatch.setattr(worker, "request_stop", stop)
    loop = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(stopping.wait(), timeout=5)
        assert not loop.done(), "另一个槽位故障不能取消正在执行的任务"
        release.set()
        with pytest.raises(RuntimeError, match="isolated database claim failure"):
            await asyncio.wait_for(loop, timeout=5)
        rows = [await service.store.get_run(run_id) for run_id in run_ids]
        assert sum(row["status"] == RunStatus.SUCCEEDED for row in rows) == 1
        assert sum(row["status"] == RunStatus.QUEUED for row in rows) == 2
    finally:
        worker.request_stop()
        release.set()
        await asyncio.gather(loop, return_exceptions=True)
        await worker.aclose()


@pytest.mark.parametrize("value", [0, -1])
def test_worker_concurrency_must_be_positive(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_concurrency=value)


async def test_backend_dispatch_mode_starts_received_jobs_without_second_limit(
    tmp_path, monkeypatch
):
    worker = await make_worker(tmp_path, None)
    service = worker.service
    started = asyncio.Queue()
    release = asyncio.Event()
    execute = worker._execute
    run_ids = [
        await service.submit(workflow.WORKFLOW_ID, workflow.default_inputs("[]"))
        for _ in range(12)
    ]

    async def held_execute(rid, *args):
        started.put_nowait(rid)
        await release.wait()
        await execute(rid, *args)

    monkeypatch.setattr(worker, "_execute", held_execute)
    loop = asyncio.create_task(worker.run_forever())
    try:
        observed = [await asyncio.wait_for(started.get(), timeout=5) for _ in run_ids]
        assert set(observed) == set(run_ids)
        assert await service.store.queue_depth() == 0
        rows = [await service.store.get_run(rid) for rid in run_ids]
        assert all(row["status"] == RunStatus.RUNNING for row in rows)
        worker.request_stop()
        assert not loop.done()
        release.set()
        await asyncio.wait_for(loop, timeout=5)
        rows = [await service.store.get_run(rid) for rid in run_ids]
        assert all(row["status"] == RunStatus.SUCCEEDED for row in rows)
    finally:
        worker.request_stop()
        release.set()
        await asyncio.gather(loop, return_exceptions=True)
        await worker.aclose()


async def test_dynamic_execution_error_stops_dispatch_and_drains_other_jobs(
    tmp_path, monkeypatch
):
    worker = await make_worker(tmp_path, None)
    service = worker.service
    first, second = [
        await service.submit(workflow.WORKFLOW_ID, workflow.default_inputs("[]"))
        for _ in range(2)
    ]
    started, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = worker._execute
    request_stop = worker.request_stop

    async def controlled_execute(rid, *args):
        if rid == first:
            started.set()
            await release.wait()
            await execute(rid, *args)
        else:
            assert rid == second
            await started.wait()
            raise RuntimeError("isolated execution failure")

    def stop():
        request_stop()
        stopping.set()

    monkeypatch.setattr(worker, "_execute", controlled_execute)
    monkeypatch.setattr(worker, "request_stop", stop)
    loop = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(stopping.wait(), timeout=5)
        assert not loop.done()
        release.set()
        with pytest.raises(RuntimeError, match="isolated execution failure"):
            await asyncio.wait_for(loop, timeout=5)
        assert (await service.store.get_run(first))["status"] == RunStatus.SUCCEEDED
    finally:
        worker.request_stop()
        release.set()
        await asyncio.gather(loop, return_exceptions=True)
        await worker.aclose()


def test_blank_worker_concurrency_selects_backend_dispatch_mode():
    assert Settings(_env_file=None, worker_concurrency="").worker_concurrency is None

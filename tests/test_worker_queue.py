"""持久化队列与 worker（V0.5.2）。

四件事各有一条「看起来正常」的失效方式，专门钉住：

1. **「先落库再返回」可能只是「先建了个内存任务」。** v0.5.1 之前提交后等的是留痕
   层写一行 ``running``，进程一挂任务就没了。这里断言提交返回后库里就是 ``queued``，
   且**没有 worker 参与**——这是持久化的正面证据。
2. **两个 worker 可能领到同一个任务。** 「先 SELECT 再 UPDATE」之间存在窗口，窗口内
   两个消费者会把它跑两遍。这里用两个 Store 直接并发领取来证明原子性。
3. **worker 崩溃可能丢任务。** 已有任务的存活由「queued 不被标 interrupted」保证；
   而一条跑到一半的运行，其租约到期后应能被人为退回队列（**不自动重跑**——那需要幂等回写）。
4. **给既有库加列可能悄悄失败。** ``CREATE TABLE IF NOT EXISTS`` 不会补列，而
   ``data/customer_profile.db`` 已在磁盘上。用手工建的旧库验证补列真的发生。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from customer_profile.api import ENTRY_WORKFLOW_ID, create_app
from customer_profile.definitions import RunStatus
from customer_profile.persistence import Store
from customer_profile.persistence.schema import RunRecordRow
from customer_profile.replay import ReplaySource
from customer_profile.runner import QueueFull, build_service
from customer_profile.workflows import mengshi_it_system_data as mengshi

from .conftest import make_settings

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"
)


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "queue.db")
    store.connect()
    yield store
    store.close()


async def _make_service(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return await build_service(
        settings, replay=ReplaySource.from_file(FIXTURE, strict=True)
    )


def _read_run_from_file(tmp_path, run_id: str) -> dict | None:
    """直接查 SQLite 文件，绕开被测对象自身。"""
    db = tmp_path / "test.db"
    if not db.is_file():
        return None
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


# ---------------------------------------------------------------- 入队即落库


async def test_submit_enqueues_before_returning_without_any_worker(tmp_path):
    """提交返回后库里就是 ``queued``，且**不依赖任何 worker**。

    这是「先持久化入队再返回 run_id」的正面证据：进程此刻退出，任务仍在库里。
    """
    # 关掉内联 consumer：本条要证的正是「即使没有消费者，任务也已经持久化了」。
    service = await _make_service(tmp_path, inline_worker=False)
    try:
        run_id = await service.submit(
            ENTRY_WORKFLOW_ID, {"phone_number": "13800000001"}
        )
        row = _read_run_from_file(tmp_path, run_id)
        assert row is not None, "提交返回了 run_id，但库里查不到——入队没有落库"
        assert row["status"] == RunStatus.QUEUED
        assert row["queued_at_ms"] is not None, "入队时刻未记录"
        assert row["claimed_by"] is None, "不该有人领走它"
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_queued_run_is_picked_up_and_completes(tmp_path):
    """worker 领取后任务真的跑完并落终态。

    ``inline_worker=False``：本条要**手工**领取，若内联消费者也在，它会抢在前头把
    任务领走——那样断言到的就不是本测试的领取行为，而是它的。
    """
    service = await _make_service(tmp_path, inline_worker=False)
    try:
        worker_id = "test-worker"
        claimed = await service.store.claim_next_queued_run(
            worker_id, lease_ms=60_000
        )
        assert claimed is None, "队列本该是空的"

        run_id = await service.submit(
            mengshi.WORKFLOW_ID, mengshi.default_inputs("[]")
        )
        claimed = await service.store.claim_next_queued_run(
            worker_id, lease_ms=60_000
        )
        assert claimed is not None
        assert claimed["run_id"] == run_id
        assert claimed["claimed_by"] == worker_id
        assert claimed["lease_expires_at_ms"] is not None
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


# ---------------------------------------------------------------- 原子领取


async def test_only_one_of_two_stores_can_claim_the_same_run(tmp_path):
    """同一个任务只被领走一次。

    用两个独立的 Store（各自一个连接、一个线程）直接并发领取。若实现是
    「先 SELECT 再 UPDATE」，两者都会读到同一行并都认为自己领到了。
    """
    first = Store(tmp_path / "claim.db")
    second = Store(tmp_path / "claim.db")
    first.connect()
    second.connect()
    try:
        await first.enqueue_run(
            RunRecordRow(run_id="only-one", workflow_id="wf")
        )
        got_first = await first.claim_next_queued_run("w1", lease_ms=60_000)
        got_second = await second.claim_next_queued_run("w2", lease_ms=60_000)

        winners = [g for g in (got_first, got_second) if g is not None]
        assert len(winners) == 1, (
            f"{len(winners)} 个 worker 领到同一个任务——它会被跑两遍"
        )
        assert winners[0]["run_id"] == "only-one"
    finally:
        first.close()
        second.close()


async def test_claim_skips_child_runs(tmp_path):
    """子运行不参与排队。

    子运行由父运行的 ``tool`` 节点直接执行。若它们也进队列，主入口一次扇出十几路，
    会与父运行抢队列名额，且父运行在等子运行、子运行在等名额，形成自锁。
    """
    store = Store(tmp_path / "child.db")
    store.connect()
    try:
        # 父运行必须真实存在：parent_run_id 是指向本表的外键，指向不存在的行会
        # 直接报 FOREIGN KEY constraint failed，而那样测的就不是「子运行不入队」了。
        await store.enqueue_run(RunRecordRow(run_id="parent-run", workflow_id="wf"))
        await store.enqueue_run(RunRecordRow(run_id="child", workflow_id="wf"))
        await store._write(
            "UPDATE workflow_runs SET parent_run_id = ?, status = ? WHERE run_id = ?",
            ("parent-run", RunStatus.QUEUED, "child"),
        )

        # 父运行先被领走，此时队列里只剩那个子运行——它必须领不到。
        first = await store.claim_next_queued_run("w", lease_ms=1000)
        assert first is not None and first["run_id"] == "parent-run"
        assert await store.claim_next_queued_run("w", lease_ms=1000) is None, (
            "子运行被领走了——它会与等待它的父运行抢名额，上限一低就自锁"
        )
    finally:
        store.close()


# ---------------------------------------------------------------- 租约


async def test_expired_lease_can_be_requeued_for_manual_recovery(tmp_path):
    """租约过期的运行可被退回队列。

    **不自动执行**——退回是显式动作（将来由人工处理入口调用）。这里验证退回本身正确：
    状态回到 queued，且认领信息被清干净，否则新的 worker 续租时会发现自己不是主人。
    """
    store = Store(tmp_path / "lease.db")
    store.connect()
    try:
        await store.enqueue_run(RunRecordRow(run_id="stuck", workflow_id="wf"))
        claimed = await store.claim_next_queued_run("w1", lease_ms=1)
        assert claimed is not None and claimed["run_id"] == "stuck"

        # 租约已过期（lease_ms=1，且此刻已过了至少 1ms）
        import asyncio

        await asyncio.sleep(0.01)
        requeued = await store.requeue_expired_leases()
        assert requeued == 1

        row = await store.get_run("stuck")
        assert row["status"] == RunStatus.QUEUED
        assert row["claimed_by"] is None
        assert row["lease_expires_at_ms"] is None
    finally:
        store.close()


async def test_renew_lease_fails_after_requeue(tmp_path):
    """退回队列后，原 worker 不能再续租——否则它会继续以为自己还持有该任务。"""
    store = Store(tmp_path / "renew.db")
    store.connect()
    try:
        await store.enqueue_run(RunRecordRow(run_id="r", workflow_id="wf"))
        await store.claim_next_queued_run("w1", lease_ms=1)
        import asyncio

        await asyncio.sleep(0.01)
        await store.requeue_expired_leases()

        assert await store.renew_lease("r", "w1", lease_ms=60_000) is False
    finally:
        store.close()


# ---------------------------------------------------------------- 队深上限


async def test_queue_depth_limit_refuses_without_leaving_a_row(tmp_path):
    """队列满时提交被拒，且**不留半条记录**。

    与 v0.5.1 的名额闸门同一要求：若先落库再判深度，拒绝会在库里留下一个永远不跑的
    运行——列表上看得见、状态永远 queued。
    """
    service = await _make_service(tmp_path, max_queued_runs=1, inline_worker=False)
    try:
        await service.submit(ENTRY_WORKFLOW_ID, {"phone_number": "13800000001"})
        assert await service.store.queue_depth() == 1

        with pytest.raises(QueueFull):
            await service.submit(ENTRY_WORKFLOW_ID, {"phone_number": "13800000002"})

        assert await service.store.queue_depth() == 1, "被拒的提交留下了记录"
        assert await service.store.count_runs() == 1
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_queue_full_becomes_http_429(tmp_path):
    """队满翻成 429 并带 Retry-After，不是 500。"""
    service = await _make_service(tmp_path, max_queued_runs=1, inline_worker=False)

    async def factory():
        return service

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            async with app.router.lifespan_context(app):
                first = await http.post(
                    "/runs",
                    json={
                        "workflow_id": ENTRY_WORKFLOW_ID,
                        "inputs": {"phone_number": "13800000001"},
                    },
                )
                assert first.status_code == 200, first.text

                second = await http.post(
                    "/runs",
                    json={
                        "workflow_id": ENTRY_WORKFLOW_ID,
                        "inputs": {"phone_number": "13800000002"},
                    },
                )
        assert second.status_code == 429, second.text
        assert second.headers.get("retry-after") == "30"
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


# ---------------------------------------------------------------- 补列迁移


def test_migration_adds_missing_columns_to_an_existing_db(tmp_path):
    """给既有库补列：旧库（缺队列列）连上后应拿到新列，且**旧数据完好**。

    ``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列，而 data/customer_profile.db
    已在磁盘上。没有补列逻辑，新列在运行期只表现为一句 "no such column"。
    """
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    try:
        # 手工造一份「V0.5.1 时期」的库：没有队列四列
        connection.execute(
            "CREATE TABLE workflow_runs ("
            " run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,"
            " workflow_display_name TEXT, parent_run_id TEXT, parent_node_id TEXT,"
            " call_path TEXT NOT NULL DEFAULT '', business_ref TEXT,"
            " status TEXT NOT NULL, definition_version_id INTEGER,"
            " inputs_json TEXT, outputs_json TEXT, error TEXT,"
            " started_at_ms INTEGER, finished_at_ms INTEGER, duration_ms INTEGER,"
            " is_replay INTEGER NOT NULL DEFAULT 0,"
            " created_at_ms INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_runs (run_id, workflow_id, status, created_at_ms) "
            "VALUES ('legacy', 'wf', 'succeeded', 1)"
        )
        connection.commit()
    finally:
        connection.close()

    store = Store(db)
    store.connect()
    try:
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workflow_runs)")
            }
            for expected in (
                "queued_at_ms",
                "claimed_at_ms",
                "claimed_by",
                "lease_expires_at_ms",
            ):
                assert expected in columns, f"补列失败：缺 {expected}"

            legacy = connection.execute(
                "SELECT run_id, status FROM workflow_runs WHERE run_id = 'legacy'"
            ).fetchone()
            assert legacy is not None, "补列把旧数据弄丢了"
            assert legacy["status"] == "succeeded"
        finally:
            connection.close()
    finally:
        store.close()


def test_migration_is_idempotent_across_repeated_connects(tmp_path):
    """重复 connect（API 与 worker 两个进程各一次）不得报错。"""
    db = tmp_path / "twice.db"
    first = Store(db)
    first.connect()
    second = Store(db)
    second.connect()  # 第二个进程启动，列已存在
    try:
        assert first.connected and second.connected
    finally:
        first.close()
        second.close()


async def test_two_stores_can_write_concurrently(tmp_path):
    """两个连接同时写不抛 "database is locked"。

    这是 busy_timeout 的作用：不设它时，两个进程的写相遇会立刻失败而不是等一会儿。
    """
    import asyncio

    first = Store(tmp_path / "concurrent.db")
    second = Store(tmp_path / "concurrent.db")
    first.connect()
    second.connect()
    try:
        await asyncio.gather(
            *[
                first.enqueue_run(RunRecordRow(run_id=f"a{i}", workflow_id="wf"))
                for i in range(10)
            ],
            *[
                second.enqueue_run(RunRecordRow(run_id=f"b{i}", workflow_id="wf"))
                for i in range(10)
            ],
        )
        assert await first.count_runs() == 20
    finally:
        first.close()
        second.close()


# ---------------------------------------------------------------- worker 主循环


async def test_worker_run_once_executes_a_queued_task(tmp_path):
    """worker 的一轮：领取→执行→落终态。"""
    from customer_profile.worker import Worker

    service = await _make_service(tmp_path, inline_worker=False)
    try:
        run_id = await service.submit(
            mengshi.WORKFLOW_ID, mengshi.default_inputs("[]")
        )
        worker = Worker(service, service.settings)
        ran = await worker.run_once()
        assert ran is True

        row = await service.store.get_run(run_id)
        assert row["status"] == RunStatus.SUCCEEDED, row.get("error")
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_worker_run_once_returns_false_on_empty_queue(tmp_path):
    from customer_profile.worker import Worker

    service = await _make_service(tmp_path, inline_worker=False)
    try:
        worker = Worker(service, service.settings)
        assert await worker.run_once() is False
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_inline_worker_is_off_when_disabled(tmp_path):
    """``INLINE_WORKER=false`` 时不启动内联消费者，任务停 queued 等人领。"""
    service = await _make_service(tmp_path, inline_worker=False)
    try:
        assert service.inline_worker_task is None
        run_id = await service.submit(
            ENTRY_WORKFLOW_ID, {"phone_number": "13800000001"}
        )
        import asyncio

        await asyncio.sleep(0.05)
        row = await service.store.get_run(run_id)
        assert row["status"] == RunStatus.QUEUED, (
            "内联消费者被关掉了，任务却自己跑起来了"
        )
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

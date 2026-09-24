"""留痕层测试：四层记录是否真的落下。

留痕最容易出的问题是「看起来记了，其实没记全」——比如只在成功路径写、或者 attempt
层被重试吞掉。这里逐层断言存在性与内容。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from customer_profile.definitions import RunStatus
from customer_profile.persistence import RunTracker, Store
from customer_profile.persistence.schema import RunRecordRow, json_dumps, json_loads
from customer_profile.replay import ReplaySource
from customer_profile.workflows import human_corrected_info as hci
from customer_profile.workflows import synthetic_fanout as syn

from .conftest import make_settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "ledger.db")
    store.connect()
    yield store
    store.close()


# ---------------------------------------------------------------- 表结构


async def test_schema_applies_idempotently(store):
    store.connect()  # 再跑一次不得报错
    # 表存在性用公开路径验证（直接读 sqlite_master 需要拿到连接，而连接由专属线程独占）
    ids = await store.ensure_definition_version("wf", {"workflow_id": "wf"})
    assert ids >= 1
    await store.insert_run(RunRecordRow(run_id="probe", workflow_id="wf"))
    assert (await store.get_run("probe")) is not None
    assert await store.count_runs() == 1


# ---------------------------------------------------------------- 版本快照


async def test_definition_version_is_deduplicated(store):
    definition = hci.WORKFLOW.asdict()
    first = await store.ensure_definition_version(hci.WORKFLOW_ID, definition)
    second = await store.ensure_definition_version(hci.WORKFLOW_ID, definition)
    assert first == second, "同一份定义重复登记应复用同一个版本"


async def test_definition_version_differs_when_definition_changes(store):
    base = hci.WORKFLOW.asdict()
    first = await store.ensure_definition_version(hci.WORKFLOW_ID, base)

    changed = dict(base)
    changed["display_name"] = "改了个名字"
    second = await store.ensure_definition_version(hci.WORKFLOW_ID, changed)
    assert first != second


async def test_definition_snapshot_keeps_full_topology(store):
    version_id = await store.ensure_definition_version(
        hci.WORKFLOW_ID, hci.WORKFLOW.asdict()
    )
    version = await store.definition_version(version_id)
    assert version["definition"]["workflow_id"] == hci.WORKFLOW_ID
    assert len(version["definition"]["nodes"]) == len(hci.WORKFLOW.nodes)
    assert len(version["definition"]["edges"]) == len(hci.WORKFLOW.edge_list())


# ---------------------------------------------------------------- 运行层


async def test_run_row_round_trips(store):
    await store.insert_run(
        RunRecordRow(
            run_id="r1",
            workflow_id="wf",
            inputs={"phone_number": "138"},
            business_ref="138",
            status=RunStatus.RUNNING,
            started_at_ms=1000,
        )
    )
    row = await store.get_run("r1")
    assert row["workflow_id"] == "wf"
    assert row["inputs_json"] == {"phone_number": "138"}
    assert row["status"] == RunStatus.RUNNING


async def test_run_finished_updates_status_and_outputs(store):
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    await store.update_run_finished(
        "r1", status=RunStatus.SUCCEEDED, outputs={"result1": "ok"}, duration_ms=42
    )
    row = await store.get_run("r1")
    assert row["status"] == RunStatus.SUCCEEDED
    assert row["outputs_json"] == {"result1": "ok"}
    assert row["duration_ms"] == 42
    assert row["finished_at_ms"] is not None


async def test_interrupted_runs_are_marked_on_startup(store):
    """进程重启：残留的 **running** 标为 interrupted，**queued 保持不动**。

    queued 是持久化队列里等待被领取的任务。若一并标成中断，「先持久化入队再返回
    run_id」就失去了意义——worker 一重启，所有还没开始的任务都变成需要人工处理的
    中断态。所以这里断言 queued **存活**，它会由 worker 下一轮领走。
    """
    await store.insert_run(
        RunRecordRow(run_id="live", workflow_id="wf", status=RunStatus.RUNNING)
    )
    await store.insert_run(
        RunRecordRow(run_id="waiting", workflow_id="wf", status=RunStatus.QUEUED)
    )
    await store.insert_run(
        RunRecordRow(run_id="done", workflow_id="wf", status=RunStatus.SUCCEEDED)
    )

    marked = await store.mark_running_as_interrupted()
    assert marked == 1, "只应标记 running"

    assert (await store.get_run("live"))["status"] == RunStatus.INTERRUPTED
    assert (await store.get_run("waiting"))["status"] == RunStatus.QUEUED, (
        "queued 被标成中断了——「worker 重启不丢任务」随之不成立"
    )
    assert (await store.get_run("done"))["status"] == RunStatus.SUCCEEDED


# ---------------------------------------------------------------- 节点层


async def test_node_execution_is_upserted_not_duplicated(store):
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    for status in ("running", "succeeded"):
        await store.upsert_node_execution(
            run_id="r1",
            node_id="n1",
            node_title="节点",
            node_type="code",
            call_path="/",
            status=status,
            outputs={"x": 1} if status == "succeeded" else None,
        )
    rows = await store.list_node_executions("r1")
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["outputs_json"] == {"x": 1}


async def test_same_node_id_in_different_call_paths_are_separate_rows(store):
    """同一静态节点 ID 在不同调用路径下是不同记录（§5.3）。"""
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    for call_path in ("/0/1", "/0/2"):
        await store.upsert_node_execution(
            run_id="r1",
            node_id="shared",
            node_title="被复用的节点",
            node_type="llm",
            call_path=call_path,
            status="succeeded",
        )
    rows = await store.list_node_executions("r1")
    assert len(rows) == 2


async def test_attempt_count_can_be_bumped(store):
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    await store.upsert_node_execution(
        run_id="r1",
        node_id="n1",
        node_title="节点",
        node_type="llm",
        call_path="/",
        status="succeeded",
    )
    await store.bump_attempt_count(run_id="r1", node_id="n1", call_path="/", count=3)
    rows = await store.list_node_executions("r1")
    assert rows[0]["attempt_count"] == 3


# ---------------------------------------------------------------- 尝试层


async def test_attempts_are_recorded_per_attempt(store):
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    for attempt_no in (1, 2, 3):
        await store.insert_attempt(
            run_id="r1",
            node_id="n1",
            call_path="/",
            attempt_no=attempt_no,
            kind="llm",
            request={"model": "m"},
            response_text=f"第{attempt_no}次",
            duration_ms=attempt_no * 10,
            usage={"total_tokens": 5},
        )
    rows = await store.list_attempts("r1")
    assert [row["attempt_no"] for row in rows] == [1, 2, 3]
    assert rows[0]["usage_json"] == {"total_tokens": 5}


async def test_attempt_keeps_unknown_usage_as_none(store):
    """usage 缺失时标为未知，不伪造 0（§6）。"""
    await store.insert_run(RunRecordRow(run_id="r1", workflow_id="wf"))
    await store.insert_attempt(
        run_id="r1",
        node_id="n1",
        call_path="/",
        attempt_no=1,
        kind="llm",
        usage=None,
    )
    rows = await store.list_attempts("r1")
    assert rows[0]["usage_json"] is None


# ---------------------------------------------------------------- 运行树


async def test_child_runs_are_linked_to_parent(store):
    await store.insert_run(RunRecordRow(run_id="parent", workflow_id="root"))
    await store.insert_run(
        RunRecordRow(run_id="child", workflow_id="sub", parent_run_id="parent", call_path="/0")
    )
    children = await store.list_child_runs("parent")
    assert [c["run_id"] for c in children] == ["child"]


async def test_default_listing_hides_children(store):
    await store.insert_run(RunRecordRow(run_id="parent", workflow_id="root"))
    await store.insert_run(
        RunRecordRow(run_id="child", workflow_id="sub", parent_run_id="parent")
    )
    top_level = await store.list_runs()
    assert [row["run_id"] for row in top_level] == ["parent"]


# ---------------------------------------------------------------- 序列化


def test_json_dumps_degrades_instead_of_raising():
    class Odd:
        def __repr__(self) -> str:
            return "<odd>"

    dumped = json_dumps({"value": Odd()})
    payload = json.loads(dumped)
    assert payload["value"]["_unserialisable"] == "Odd"


def test_json_loads_tolerates_garbage():
    assert json_loads("{不是 JSON}") == {"_unparsable": "{不是 JSON}"}
    assert json_loads(None) is None


# ---------------------------------------------------------------- 端到端留痕


async def test_full_run_records_all_four_layers(tmp_path):
    """跑一次真实工作流，断言四层记录都在。"""
    from customer_profile.runner import build_service

    settings = make_settings(tmp_path)
    replay = ReplaySource.from_file(FIXTURE, strict=True)
    service = await build_service(settings, replay=replay)
    try:
        outcome = await service.run(
            hci.WORKFLOW_ID, hci.default_inputs("13800000001"), business_ref="13800000001"
        )
        assert outcome.status == RunStatus.SUCCEEDED

        # 1. 工作流运行
        run = await service.store.get_run(outcome.run_id)
        assert run["status"] == RunStatus.SUCCEEDED
        assert run["inputs_json"] == {"phone_number": "13800000001"}
        assert run["outputs_json"]["result"]

        # 2. 节点执行
        nodes = await service.store.list_node_executions(outcome.run_id)
        assert len(nodes) == len(hci.WORKFLOW.nodes)
        assert all(node["queued_at_ms"] is not None for node in nodes)

        # 3. 请求尝试
        attempts = await service.store.list_attempts(outcome.run_id)
        assert attempts and attempts[0]["kind"] == "http"

        # 4. 版本快照
        assert run["definition_version_id"] is not None
    finally:
        await service.aclose()
        service.store.close()


async def test_child_run_records_its_own_nodes(tmp_path):
    """子运行有自己的节点记录与 parent 关联，可下钻。"""
    from customer_profile.runner import build_service

    settings = make_settings(tmp_path)
    service = await build_service(settings, replay=ReplaySource(strict=False))
    try:
        outcome = await service.run(syn.WORKFLOW_ID, syn.default_inputs())
        children = await service.store.list_child_runs(outcome.run_id)
        assert children, "没有子运行"

        child = children[0]
        assert child["workflow_id"] == syn.CHILD_WORKFLOW_ID
        assert child["parent_run_id"] == outcome.run_id
        assert child["call_path"].startswith("/")

        child_nodes = await service.store.list_node_executions(child["run_id"])
        assert {n["node_id"] for n in child_nodes} == {
            node.node_id for node in syn.CHILD_WORKFLOW.nodes
        }
    finally:
        await service.aclose()
        service.store.close()


async def test_tracker_fails_soft_by_default(store, capsys):
    """留痕写库失败不阻断业务，但必须报出来，不能静默丢痕迹。"""
    tracker = RunTracker(store, fail_soft=True)
    store.close()  # 制造写库失败

    class DummyRun:
        run_id = "r1"
        workflow = hci.WORKFLOW
        workflow_id = hci.WORKFLOW_ID
        workflow_display_name = hci.WORKFLOW.display_name
        parent_run_id = None
        parent_node_id = None
        call_path = "/"
        business_ref = None
        inputs = {}
        started_at_ms = 0
        executions: list = []
        node_started_at: dict = {}

    await tracker.run_started(DummyRun())  # 不应抛异常
    captured = capsys.readouterr()
    assert "留痕" in captured.err


async def test_tracker_fails_loud_when_asked(store):
    tracker = RunTracker(store, fail_soft=False)
    store.close()

    class DummyRun:
        run_id = "r1"
        workflow = hci.WORKFLOW
        workflow_id = hci.WORKFLOW_ID
        workflow_display_name = hci.WORKFLOW.display_name
        parent_run_id = None
        parent_node_id = None
        call_path = "/"
        business_ref = None
        inputs = {}
        started_at_ms = 0
        executions: list = []
        node_started_at: dict = {}

    with pytest.raises(RuntimeError, match="留痕"):
        await tracker.run_started(DummyRun())

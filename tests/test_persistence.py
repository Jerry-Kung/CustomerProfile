"""留痕层测试：四层记录是否真的落下。

留痕最容易出的问题是「看起来记了，其实没记全」——比如只在成功路径写、或者 attempt
层被重试吞掉。这里逐层断言存在性与内容。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from loguru import logger

from customer_profile.definitions import NodeStatus, RunStatus
from customer_profile.persistence import RunTracker, Store
from customer_profile.persistence.schema import SCHEMA_STATEMENTS, RunRecordRow, json_dumps, json_loads
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


async def test_mysql_queue_reads_release_the_transaction_snapshot(monkeypatch):
    committed_status = RunStatus.QUEUED
    snapshot = None

    def start_read():
        nonlocal snapshot
        if snapshot is None:
            snapshot = committed_status
        return snapshot

    def read_queue(sql, params):
        cursor = Mock()
        cursor.fetchall.return_value = [{"status": start_read(), "n": 1}]
        return cursor

    def end_transaction():
        nonlocal snapshot
        snapshot = None

    connection = Mock()
    connection.execute.side_effect = read_queue
    connection.rollback.side_effect = end_transaction
    backend = SimpleNamespace(name="mysql", connect=lambda: connection)
    monkeypatch.setattr(
        "customer_profile.persistence.mysql_schema.assert_expected_version",
        lambda connection: start_read(),
    )

    reader = Store(backend)
    reader.connect()
    try:
        committed_status = RunStatus.RUNNING
        assert (await reader.queue_snapshot(workflow_scope="profile"))["running"] == 1
        committed_status = RunStatus.SUCCEEDED
        queue = await reader.queue_snapshot(workflow_scope="profile")
        assert queue == {"queued": 0, "running": 0, "interrupted": 0}
        assert snapshot is None
    finally:
        reader.close()


async def test_business_upgrade_preserves_legacy_history_and_adds_attempt_columns(tmp_path):
    """旧卷中的历史入口记录与快照保留，两进程重连后仍能写入新的异常字段。"""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        assert "anomaly" not in {
            row[1] for row in connection.execute("PRAGMA table_info(request_attempts)")
        }
        connection.execute(
            "INSERT INTO definition_versions "
            "(workflow_id, definition_hash, definition_json, created_at_ms) "
            "VALUES (?, ?, ?, ?)",
            ("customer_profile_entry", "historical", '{"display_name":"历史入口"}', 1),
        )
        connection.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, status, definition_version_id, outputs_json, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old-run", "customer_profile_entry", "succeeded", 1,
             '{"result1":"historical-result"}', 1),
        )
    stores = [Store(path), Store(path)]
    try:
        for ledger in stores:
            ledger.connect()
        old = await stores[0].get_run("old-run")
        assert old["outputs_json"] == {"result1": "historical-result"}
        assert (await stores[1].definition_version(1))["definition"] == {"display_name": "历史入口"}
        await stores[1].insert_attempt(
            run_id="old-run", node_id="probe", call_path="", attempt_no=1,
            kind="llm", anomaly="empty_text", finish_reason="length",
        )
        attempts = await stores[0].list_attempts("old-run")
        assert attempts[0]["anomaly"] == "empty_text"
        assert attempts[0]["finish_reason"] == "length"
    finally:
        for ledger in stores:
            ledger.close()


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

    sink_id = logger.add(lambda message: sys.stderr.write(str(message)), diagnose=False)
    try:
        await tracker.run_started(DummyRun())  # 不应抛异常
        captured = capsys.readouterr()
    finally:
        logger.remove(sink_id)
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


# ---------------------------------------------------------------- none 档


class _StubRun:
    """tracker 需要的最小运行对象。字段与调度层的 ``Run`` 同名。"""

    def __init__(self, *, run_id: str = "r1", parent_run_id: str | None = None) -> None:
        self.run_id = run_id
        self.workflow = hci.WORKFLOW
        self.workflow_id = hci.WORKFLOW_ID
        self.workflow_display_name = hci.WORKFLOW.display_name
        self.parent_run_id = parent_run_id
        self.parent_node_id = None
        self.call_path = "/"
        self.business_ref = None
        self.inputs = {}
        self.started_at_ms = 0
        self.executions: list = []
        self.node_started_at: dict = {}


async def _none_tracker(store) -> RunTracker:
    """``none`` 档的 tracker：不写载荷、不写包。"""
    store.connect()
    return RunTracker(store, persist_mode="none", fail_soft=False)


async def test_none_mode_does_not_write_run_payloads(store):
    """``none`` 档连子运行的 ``outputs_json`` 都不写——那是可重跑的中间结果。"""
    tracker = await _none_tracker(store)
    from customer_profile.execution.scheduler import RunOutcome

    outcomes = {
        "root": _StubRun(run_id="root"),
        "child": _StubRun(run_id="child", parent_run_id="root"),
    }
    for run in outcomes.values():
        await tracker.run_started(run)
    for run_id, run in outcomes.items():
        await tracker.run_finished(
            run,
            RunOutcome(
                run_id=run_id,
                workflow_id=run.workflow_id,
                status=RunStatus.SUCCEEDED,
                outputs={"result1": "{}"},
                duration_ms=1,
            ),
        )

    rows = await store._read("SELECT run_id, outputs_json, payload_state FROM workflow_runs")
    by_run = {row["run_id"]: row for row in rows}
    # 根运行的最终影像是这一档唯一的载荷例外：profile 接口给不了「当时写回了什么」。
    assert json_loads(by_run["root"]["outputs_json"]) == {"result1": "{}"}
    # 子运行只有骨架。
    assert by_run["child"]["outputs_json"] is None
    # 载荷状态如实标 skeleton，读侧据此说「按档位不留」而不是「还在库里」。
    assert {row["payload_state"] for row in rows} == {"skeleton"}


async def test_none_mode_drops_node_and_attempt_payloads(store):
    tracker = await _none_tracker(store)
    await tracker.run_started(_StubRun())

    class _Node:
        node_id = "n1"
        title = "节点"
        node_type = "llm"
        bindings: list = []

    class _Execution:
        node_id = "n1"
        title = "节点"
        node_type = "llm"
        status = RunStatus.SUCCEEDED
        outputs = {"facts_json": {"a": 1}}
        error = None
        queued_at_ms = 0
        started_at_ms = 0
        duration_ms = 5
        branch = None
        sub_run_id = None
        call_path = ""

    await tracker.node_started(_StubRun(), _Node())
    await tracker.node_finished(_StubRun(), _Execution())

    nodes = await store._read("SELECT status, inputs_json, outputs_json FROM node_executions")
    assert len(nodes) == 1
    # 骨架照留：状态与耗时是「哪一步失败」的唯一依据。
    assert nodes[0]["status"] == RunStatus.SUCCEEDED
    assert nodes[0]["inputs_json"] is None
    assert nodes[0]["outputs_json"] is None


async def test_none_mode_never_writes_package(store, tmp_path):
    """``none`` 档不写包——它连载荷都不写，没有可搬的东西。"""
    tracker = RunTracker(
        store,
        persist_mode="none",
        archive_dir=tmp_path / "archives",
        fail_soft=False,
    )
    store.connect()
    run = _StubRun(run_id="root")
    await tracker.run_started(run)
    from customer_profile.execution.scheduler import RunOutcome

    await tracker.run_finished(
        run,
        RunOutcome(
            run_id="root",
            workflow_id=run.workflow_id,
            status=RunStatus.SUCCEEDED,
            outputs={"result1": "{}"},
            duration_ms=1,
        ),
    )
    assert not list((tmp_path / "archives").rglob("*.json.gz"))


async def test_none_mode_skeleton_exports_as_skeleton(store):
    """导出头要如实说「这批不含运行时数据」，与 purged 同一档。"""
    tracker = await _none_tracker(store)
    run = _StubRun(run_id="root")
    await tracker.run_started(run)
    from customer_profile.execution.scheduler import RunOutcome

    await tracker.run_finished(
        run,
        RunOutcome(
            run_id="root",
            workflow_id=run.workflow_id,
            status=RunStatus.SUCCEEDED,
            outputs={"result1": "{}"},
            duration_ms=1,
        ),
    )
    assert await store.payload_kinds(["root"]) == {"skeleton"}


class _StubNode:
    def __init__(self, node_id: str = "n1", node_type: str = "code") -> None:
        self.node_id = node_id
        self.title = "节点"
        self.node_type = node_type
        self.bindings: list = []


def _exec(**kw):
    """一个节点执行结果。缺省是「子运行里成功、无调用、无分支」。"""
    from customer_profile.execution.scheduler import NodeExecution

    base = dict(
        node_id="n1",
        title="节点",
        node_type="code",
        status=NodeStatus.SUCCEEDED,
        outputs={"x": 1},
        queued_at_ms=0,
        started_at_ms=0,
        duration_ms=5,
        branch=None,
        sub_run_id=None,
        call_path="",
    )
    base.update(kw)
    return NodeExecution(**base)


async def _nodes(store, run_id: str) -> list[dict]:
    return await store._read(
        "SELECT node_id, status, branch FROM node_executions WHERE run_id = ?", (run_id,)
    )


async def _seed_tree(tracker, store) -> None:
    """先建根运行再建子运行：子行有外键约束，父行必须先存在。"""
    await tracker.run_started(_StubRun(run_id="root"))
    await tracker.run_started(_StubRun(run_id="child", parent_run_id="root"))


async def test_none_mode_drops_evidence_free_child_rows(store):
    """子运行里「成功、无调用、无分支、无子运行」的行整行不留。"""
    tracker = await _none_tracker(store)
    await _seed_tree(tracker, store)
    child = _StubRun(run_id="child", parent_run_id="root")

    await tracker.node_started(child, _StubNode())
    await tracker.node_finished(child, _exec())

    assert await _nodes(store, "child") == []


async def test_none_mode_keeps_root_rows_regardless(store):
    """根运行的节点行一律保留——根图是排障第一眼要看的东西。"""
    tracker = await _none_tracker(store)
    root = _StubRun(run_id="root")
    await tracker.run_started(root)

    await tracker.node_started(root, _StubNode())
    await tracker.node_finished(root, _exec())

    rows = await _nodes(store, "root")
    assert len(rows) == 1 and rows[0]["status"] == NodeStatus.SUCCEEDED


async def test_none_mode_keeps_branch_rows(store):
    """带 ``branch`` 的行必须留：它是「这个分支为什么没走」的唯一答案。"""
    tracker = await _none_tracker(store)
    await _seed_tree(tracker, store)
    child = _StubRun(run_id="child", parent_run_id="root")

    await tracker.node_started(child, _StubNode(node_type="if-else"))
    await tracker.node_finished(child, _exec(node_type="if-else", branch="false"))

    rows = await _nodes(store, "child")
    assert len(rows) == 1 and rows[0]["branch"] == "false"


async def test_none_mode_keeps_failed_and_subrun_rows(store):
    """失败行与父子链接行都要留。"""
    tracker = await _none_tracker(store)
    await _seed_tree(tracker, store)
    child = _StubRun(run_id="child", parent_run_id="root")

    await tracker.node_started(child, _StubNode("bad"))
    await tracker.node_finished(child, _exec(node_id="bad", status=NodeStatus.FAILED))
    await tracker.node_started(child, _StubNode("spawn"))
    await tracker.node_finished(child, _exec(node_id="spawn", sub_run_id="grand"))

    assert {r["node_id"] for r in await _nodes(store, "child")} == {"bad", "spawn"}


async def test_none_mode_drops_skipped_rows(store):
    """``skipped`` 行不留：分支是否走到由 ``branch`` 行回答，它不提供额外信息。"""
    tracker = await _none_tracker(store)
    await _seed_tree(tracker, store)
    child = _StubRun(run_id="child", parent_run_id="root")

    await tracker.node_started(child, _StubNode())
    await tracker.node_finished(
        child,
        _exec(status=NodeStatus.SKIPPED, error="前置 n0 未具备分支条件，节点未执行"),
    )

    assert await _nodes(store, "child") == []


async def test_none_mode_root_output_is_compressed(store):
    """根输出是该档唯一保留的载荷，入库前压缩；读侧透明解出。"""
    tracker = await _none_tracker(store)
    root = _StubRun(run_id="root")
    await tracker.run_started(root)
    big = {"result1": "回写内容" * 500}
    from customer_profile.execution.scheduler import RunOutcome

    await tracker.run_finished(
        root,
        RunOutcome(
            run_id="root",
            workflow_id=root.workflow_id,
            status=RunStatus.SUCCEEDED,
            outputs=big,
            duration_ms=1,
        ),
    )

    raw = await store._read("SELECT outputs_json FROM workflow_runs WHERE run_id = ?", ("root",))
    stored = raw[0]["outputs_json"]
    assert stored.startswith("gzip:"), "根输出应压缩存储"
    assert len(stored) * 3 < len(json.dumps(big, ensure_ascii=False))

    run = await store.get_run("root")
    assert run["outputs_json"] == big


async def test_plain_and_packed_outputs_coexist(store):
    """同一列里明文与压缩两种形态都必须读得出——历史行不是这一档写的。"""
    await store.insert_run(RunRecordRow(run_id="old", workflow_id="wf"))
    await store.update_run_finished(
        "old", status=RunStatus.SUCCEEDED, outputs={"legacy": True}, duration_ms=1
    )
    run = await store.get_run("old")
    assert run["outputs_json"] == {"legacy": True}


async def test_none_mode_drops_usage_and_provider_request_id(store):
    """``none`` 档不留 ``usage_json`` / ``provider_request_id``（方案 §4 / §7）。

    这两个字段在方案里是 ``high`` / ``low`` 才留的：``none`` 连包都不写，没有
    「用用量对账」的场景，留着只是白占列宽。实现上曾漏判——载荷都丢了，这两个
    却照写，于是该档每行白背约 70 B；档位语义也对不上文档。
    """
    tracker = await _none_tracker(store)
    root = _StubRun(run_id="root")
    await tracker.run_started(root)

    class _Ref:
        run_id = "root"
        node_id = "n1"
        call_path = ""

    class _LlmAttempt:
        attempt_no = 1
        model = "m"
        request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        response_text = "回答"
        duration_ms = 12
        error = None
        error_code = None
        provider_request_id = "req-abc"
        anomaly = None
        finish_reason = "stop"
        replayed = False
        usage = type(
            "U", (), {"asdict": lambda self: {"prompt_tokens": 10, "completion_tokens": 5}}
        )()

        def asdict(self):
            return {"attempt_no": 1, "request": self.request, "response_text": "回答"}

    await tracker.attempt_recorded(_Ref(), _LlmAttempt())

    rows = await store._read(
        "SELECT request_json, response_text, usage_json, provider_request_id "
        "FROM request_attempts WHERE run_id = 'root'"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["request_json"] is None and row["response_text"] is None
    assert row["usage_json"] is None, "none 档不应写 usage_json"
    assert row["provider_request_id"] is None, "none 档不应写 provider_request_id"


# ---------------------------------------------------------------- 线程生命周期回归


async def test_cancelled_await_does_not_kill_the_connection_thread(tmp_path):
    """协程被取消不得让数据库线程退出。

    2026-11 UAT 的长时间积压即由此而来：``Store._run`` 用 ``asyncio.wrap_future``
    接回协程，取消会链式传到内层 ``Future``；若取消发生在 ``work()`` 执行期间，
    工作线程回写结果时抛 ``InvalidStateError``，异常分支再对同一个已取消对象
    ``set_exception`` 再抛一次——那次没有外层捕获，线程死亡且跳过 ``connection.close()``。

    线程一死，``_require_thread`` 就抛「留痕读写需要活动连接」，而进程仍存活：
    容器 Up、API 健康检查照常通过，积压无人处理。本用例钉住「取消后线程仍活着」。
    """
    import threading

    store = Store(tmp_path / "ledger.db")
    store.connect()
    try:
        started = threading.Event()
        release = threading.Event()

        def slow_work(connection):
            started.set()
            release.wait(timeout=5)
            return "done"

        task = asyncio.create_task(store._run(slow_work))
        # 等到「work 已在执行」，此时取消才落在回写阶段——正是缺陷的触发窗口。
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 放行 work，让它执行完并尝试回写结果。
        release.set()
        await asyncio.sleep(0.5)

        assert store._thread is not None
        assert store._thread.alive, "取消后数据库线程已退出"
        # 线程必须还能服务后续读写，而不是只剩一个「活着但不可用」的壳。
        version_id = await store.ensure_definition_version("wf-after-cancel", {"a": 1})
        assert version_id >= 1
    finally:
        store.close()


async def test_thread_survives_repeated_cancellation(tmp_path):
    """连续多次取消不得累积伤害：健康检查靠线程长期存活来保命。"""
    import threading

    store = Store(tmp_path / "ledger.db")
    store.connect()
    try:
        for _ in range(5):
            started = threading.Event()
            release = threading.Event()

            def slow_work(connection):
                started.set()
                release.wait(timeout=5)
                return 1

            task = asyncio.create_task(store._run(slow_work))
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.sleep(0.1)

        assert store._thread is not None and store._thread.alive
        assert await store.ensure_definition_version("wf", {"a": 1}) >= 1
    finally:
        store.close()

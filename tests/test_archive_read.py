"""读路径回填：包在、包丢了、载荷被清理，三种情形各自的返回。

关键断言不是「字段对得上」，而是**接口不能把三种情形混成同一种空值**——
「这个节点没有输出」与「输出在包里读不到」在对客排障时是完全不同的结论。
"""

from __future__ import annotations

import pytest

from customer_profile.persistence import Store
from customer_profile.persistence import archive
from customer_profile.persistence.schema import RunRecordRow


def _run(run_id: str = "root", *, parent: str | None = None) -> RunRecordRow:
    return RunRecordRow(
        run_id=run_id,
        workflow_id="customer_profile_pipeline" if parent is None else "sub_flow",
        parent_run_id=parent,
        business_ref="cdp:13800000000:B1",
        status="succeeded",
        inputs={"phone_number": "13800000000"},
        created_at_ms=1_762_300_000_000,
        started_at_ms=1_762_300_000_100,
    )


async def _seed(store: Store, *, with_child: bool = True) -> None:
    await store.insert_run(_run())
    await store.update_run_finished(
        "root", status="succeeded", outputs={"result1": '{"ok":true}'}, duration_ms=50
    )
    await store.upsert_node_execution(
        run_id="root",
        node_id="pipeline_l1_facts",
        node_title="事实层",
        node_type="llm",
        call_path="",
        status="succeeded",
        inputs={"prompt": "p"},
        outputs={"facts_json": {"brand": "x"}, "detail": "d" * 200},
        started_at_ms=1_762_300_000_300,
        duration_ms=20,
    )
    await store.upsert_node_execution(
        run_id="root",
        node_id="end",
        node_title="出口",
        node_type="end",
        call_path="",
        status="succeeded",
        inputs={},
        outputs={"result1": '{"ok":true}'},
        started_at_ms=1_762_300_000_400,
        duration_ms=1,
    )
    await store.insert_attempt(
        run_id="root",
        node_id="pipeline_l1_facts",
        call_path="",
        attempt_no=1,
        kind="llm",
        target="model-x",
        request={"messages": [{"role": "user", "content": "hi"}]},
        response_text="answer",
        usage={"total_tokens": 12},
        duration_ms=20,
    )
    if with_child:
        await store.insert_run(_run("child", parent="root"))
        await store.upsert_node_execution(
            run_id="child",
            node_id="shot",
            node_title="截图",
            node_type="http",
            call_path="",
            status="succeeded",
            inputs={},
            outputs={"summary": "child-summary"},
            started_at_ms=1_762_300_001_000,
            duration_ms=5,
        )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "trace.db", archive_dir=tmp_path / "archives")
    store.connect()
    yield store
    store.close()


# ---------------------------------------------------------------- in_db


async def test_in_db_reads_columns_directly(store):
    await _seed(store)
    row = await store.get_run("root")
    assert row["payload_state"] == "in_db"
    assert row["payload_missing"] is False
    nodes = await store.list_node_executions("root")
    facts = next(n for n in nodes if n["node_id"] == "pipeline_l1_facts")
    assert facts["outputs_json"]["detail"] == "d" * 200


# ---------------------------------------------------------------- packed


async def test_packed_reads_are_identical_to_pre_pack(tmp_path, store):
    await _seed(store)
    before_nodes = await store.list_node_executions("root")
    before_attempts = await store.list_attempts("root")
    before_run = await store.get_run("root")

    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")

    after_nodes = await store.list_node_executions("root")
    after_attempts = await store.list_attempts("root")
    after_run = await store.get_run("root")

    assert after_run["payload_state"] == "packed"
    assert after_run["outputs_json"] == before_run["outputs_json"]
    assert after_run["inputs_json"] == before_run["inputs_json"]

    for before, after in zip(before_nodes, after_nodes):
        assert after["inputs_json"] == before["inputs_json"]
        assert after["outputs_json"] == before["outputs_json"]
    assert after_attempts[0]["request_json"] == before_attempts[0]["request_json"]
    assert after_attempts[0]["response_text"] == before_attempts[0]["response_text"]


async def test_end_node_falls_back_to_run_output(tmp_path, store):
    """``end`` 的输出不入包，读回时用运行输出补，不能显示成空。"""
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    nodes = await store.list_node_executions("root")
    end = next(n for n in nodes if n["node_type"] == "end")
    assert end["outputs_json"] == {"result1": '{"ok":true}'}


async def test_child_run_is_backfilled_from_same_package(tmp_path, store):
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    nodes = await store.list_node_executions("child")
    assert nodes[0]["outputs_json"] == {"summary": "child-summary"}


async def test_low_marks_truncated_rows(tmp_path, store):
    await _seed(store)
    await store.finalize_tree("root", mode="low", archive_dir=tmp_path / "archives")
    nodes = await store.list_node_executions("root")
    facts = next(n for n in nodes if n["node_id"] == "pipeline_l1_facts")
    assert facts["payload_truncated"] == "low"
    assert set(facts["outputs_json"]) == {"facts_json"}


async def test_execution_trace_is_backfilled(tmp_path, store):
    """导出走这一条：整棵树回填后才能落成 JSONL。"""
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    trace = await store.execution_trace("root")
    assert trace["payload_state"] == "packed"
    assert trace["payload_missing"] is False
    facts = next(n for n in trace["runs"]["root"]["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert facts["outputs_json"]["detail"] == "d" * 200
    assert trace["runs"]["root"]["attempts"][0]["request_json"] is not None


# ---------------------------------------------------------------- 包缺失


async def test_missing_package_is_reported_not_swallowed(tmp_path, store):
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    for path in (tmp_path / "archives").rglob("*.json.gz"):
        path.unlink()

    row = await store.get_run("root")
    assert row["payload_state"] == "packed"
    assert row["payload_missing"] is True

    nodes = await store.list_node_executions("root")
    assert nodes and all(node.get("payload_missing") for node in nodes)

    attempts = await store.list_attempts("root")
    assert attempts and all(item.get("payload_missing") for item in attempts)


async def test_store_without_archive_dir_marks_missing(tmp_path):
    """本地复现漏配 ``TRACE_ARCHIVE_DIR`` 时，表现为「证据缺失」而不是「没有产出」。"""
    seeded = Store(tmp_path / "trace.db", archive_dir=tmp_path / "archives")
    seeded.connect()
    try:
        await _seed(seeded)
        await seeded.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    finally:
        seeded.close()

    blind = Store(tmp_path / "trace.db")
    blind.connect()
    try:
        row = await blind.get_run("root")
        assert row["payload_state"] == "packed"
        assert row["payload_missing"] is True
    finally:
        blind.close()


# ---------------------------------------------------------------- purged


async def test_purged_returns_skeleton_with_root_output(store):
    await _seed(store)
    await store.purge_tree_payloads("root")

    row = await store.get_run("root")
    assert row["payload_state"] == "purged"
    assert row["payload_missing"] is False
    assert row["outputs_json"] == {"result1": '{"ok":true}'}

    nodes = await store.list_node_executions("root")
    assert all(not node["outputs_json"] for node in nodes)
    assert all(not node.get("payload_missing") for node in nodes)


# ---------------------------------------------------------------- 缓存


async def test_package_cache_is_bounded(tmp_path, store):
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    for _ in range(10):
        await store.list_node_executions("root")
    assert len(store._payload_cache) <= store._payload_cache_size


async def test_corrupt_package_does_not_raise(tmp_path, store):
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    for path in (tmp_path / "archives").rglob("*.json.gz"):
        path.write_bytes(b"not a gzip")

    row = await store.get_run("root")
    assert row["payload_missing"] is True
    assert row["payload_state"] == "packed"

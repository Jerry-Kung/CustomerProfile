"""收尾搬运：打包成功后清库、失败时库内载荷不动、两档矩阵。

这里用真实的 ``Store`` 与临时库，不跑工作流——搬运的职责是「读树 → 裁 → 写盘 → 清库」，
与节点怎么算出来无关。手工插入的树能让断言精确到某一列。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.persistence import Store
from customer_profile.persistence import archive
from customer_profile.persistence.schema import RunRecordRow


def _run(root: str = "root", *, parent: str | None = None) -> RunRecordRow:
    return RunRecordRow(
        run_id=root,
        workflow_id="customer_profile_pipeline" if parent is None else "sub_flow",
        parent_run_id=parent,
        business_ref="cdp:13800000000:B1",
        status="succeeded",
        inputs={"phone_number": "13800000000"},
        created_at_ms=1_762_300_000_000,
        started_at_ms=1_762_300_000_100,
    )


async def _seed(store: Store, *, with_child: bool = True) -> None:
    """一棵最小但形状完整的树：根 + 一个子运行，各带节点与尝试。"""
    await store.insert_run(_run())
    await store.update_run_finished(
        "root", status="succeeded", outputs={"result1": '{"ok":true}'}, duration_ms=50
    )
    await store.upsert_node_execution(
        run_id="root",
        node_id="pipeline_l0_records",
        node_title="取数记录",
        node_type="code",
        call_path="",
        status="succeeded",
        inputs={"phone": "13800000000"},
        outputs={"records_json": {"a": "b" * 5000}},
        started_at_ms=1_762_300_000_200,
        duration_ms=10,
    )
    await store.upsert_node_execution(
        run_id="root",
        node_id="pipeline_l1_facts",
        node_title="事实层",
        node_type="llm",
        call_path="",
        status="succeeded",
        inputs={},
        outputs={"facts_json": {"brand": "x"}, "noise": "n" * 300},
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
            outputs={"summary": "s"},
            started_at_ms=1_762_300_001_000,
            duration_ms=5,
        )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "trace.db", archive_dir=tmp_path / "archives")
    store.connect()
    yield store
    store.close()


# ---------------------------------------------------------------- 两档矩阵


async def test_high_clears_library_payloads_and_writes_package(store, tmp_path):
    await _seed(store)
    written = await store.finalize_tree(
        "root", mode="high", archive_dir=tmp_path / "archives", compress_level=6
    )

    package = archive.read_package(written["path"])
    assert package["manifest"]["persist_mode"] == "high"
    assert package["manifest"]["counts"] == {"runs": 2, "nodes": 4, "attempts": 1}

    raw = await store._read("SELECT * FROM workflow_runs")
    assert all(row["payload_state"] == "packed" for row in raw)
    assert all(row["outputs_json"] is None for row in raw)

    nodes = await store._read("SELECT * FROM node_executions")
    assert all(node["inputs_json"] is None and node["outputs_json"] is None for node in nodes)

    attempts = await store._read("SELECT * FROM request_attempts")
    assert all(
        attempt["request_json"] is None
        and attempt["response_text"] is None
        and attempt["usage_json"] is None
        for attempt in attempts
    )


async def test_low_drops_requests_and_slices_outputs(store, tmp_path):
    await _seed(store)
    written = await store.finalize_tree(
        "root", mode="low", archive_dir=tmp_path / "archives"
    )
    package = archive.read_package(written["path"])
    assert package["manifest"]["persist_mode"] == "low"

    attempt = package["attempts"][0]
    assert attempt["request_json"] is None
    assert attempt["response_text"] == "answer"

    facts = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert set(facts["outputs_json"]) == {"facts_json"}
    assert "noise" not in facts["outputs_json"]

    l0 = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l0_records")
    assert l0["outputs_json"]["records_json"]["a"] == "b" * 5000


async def test_package_keeps_root_output(store, tmp_path):
    await _seed(store)
    written = await store.finalize_tree(
        "root", mode="high", archive_dir=tmp_path / "archives"
    )
    package = archive.read_package(written["path"])
    root = next(r for r in package["runs"] if r["run_id"] == "root")
    assert root["outputs"]["result1"] == '{"ok":true}'


# ---------------------------------------------------------------- 失败保护


async def test_write_failure_keeps_payloads_in_db(store, tmp_path, monkeypatch):
    """包写不出来时**不能**清库——否则唯一副本就没了。"""
    await _seed(store)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(archive, "write_package", boom)
    with pytest.raises(OSError):
        await store.finalize_tree(
            "root", mode="high", archive_dir=tmp_path / "archives"
        )

    rows = await store._read("SELECT * FROM workflow_runs")
    assert all(row["payload_state"] is None for row in rows)
    assert all(row["outputs_json"] is not None for row in rows)

    nodes = await store._read("SELECT * FROM node_executions")
    assert all(node["outputs_json"] is not None for node in nodes)

    attempts = await store._read("SELECT * FROM request_attempts")
    assert attempts[0]["request_json"] is not None


async def test_package_write_leaves_no_partial_file(store, tmp_path, monkeypatch):
    await _seed(store)

    def boom(package, path, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(archive, "write_package", boom)
    with pytest.raises(OSError):
        await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    assert not list((tmp_path / "archives").rglob("*.json.gz"))


# ---------------------------------------------------------------- 边界


async def test_missing_tree_raises_lookup_error(store, tmp_path):
    with pytest.raises(LookupError):
        await store.finalize_tree("nope", mode="high", archive_dir=tmp_path / "a")


async def test_child_rows_are_marked_packed_too(store, tmp_path):
    """直接按子运行 run_id 打开详情时，也必须知道它的载荷在包里。"""
    await _seed(store)
    await store.finalize_tree("root", mode="high", archive_dir=tmp_path / "archives")
    rows = await store._read("SELECT run_id, payload_state FROM workflow_runs")
    assert {row["run_id"]: row["payload_state"] for row in rows} == {
        "root": "packed",
        "child": "packed",
    }


async def test_archive_day_directory_is_used(store, tmp_path):
    await _seed(store)
    written = await store.finalize_tree(
        "root", mode="high", archive_dir=tmp_path / "archives"
    )
    assert len(Path(written["path"]).parent.name) == 10  # 日期目录


# ---------------------------------------------------------------- 存量清理


async def test_purge_clears_payloads_but_keeps_root_output(store):
    await _seed(store)
    count = await store.purge_tree_payloads("root")
    assert count == 2

    rows = await store._read("SELECT run_id, payload_state, outputs_json FROM workflow_runs")
    by_run = {row["run_id"]: row for row in rows}
    assert by_run["root"]["payload_state"] == "purged"
    assert json.loads(by_run["root"]["outputs_json"]) == {"result1": '{"ok":true}'}
    assert by_run["child"]["outputs_json"] is None

    nodes = await store._read("SELECT outputs_json FROM node_executions")
    assert all(node["outputs_json"] is None for node in nodes)


async def test_purge_is_idempotent_and_keeps_skeleton(store):
    await _seed(store)
    await store.purge_tree_payloads("root")
    before = await store._read("SELECT run_id, workflow_id, status, duration_ms FROM workflow_runs")
    await store.purge_tree_payloads("root")
    after = await store._read("SELECT run_id, workflow_id, status, duration_ms FROM workflow_runs")
    assert [dict(r) for r in before] == [dict(r) for r in after]


async def test_purge_writes_no_package(store, tmp_path):
    await _seed(store)
    await store.purge_tree_payloads("root")
    assert not list((tmp_path / "archives").rglob("*.json.gz"))

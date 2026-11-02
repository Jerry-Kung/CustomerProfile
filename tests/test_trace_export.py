"""完整 trace 导出：真实 SQLite、跨范围查询、深层子运行和下载中断，不调用外部服务。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import httpx
import pytest

from customer_profile.api import create_app
from customer_profile.persistence.schema import RunRecordRow
from customer_profile.runner import build_service

from .conftest import make_settings


@pytest.fixture
async def trace_env(tmp_path):
    service = await build_service(make_settings(
        tmp_path, _env_file=None, inline_worker=False, workflow_scope="profile",
    ))

    async def factory():
        return service

    app = create_app(service_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://trace-test",
        ) as client:
            yield client, service


async def add_run(store, run_id, *, workflow="invitation_strategy", parent=None,
                  created=100, status="succeeded", phone="13800000000", version=None):
    await store.insert_run(RunRecordRow(
        run_id=run_id, workflow_id=workflow, parent_run_id=parent,
        business_ref=phone, status=status, created_at_ms=created,
        definition_version_id=version,
        inputs={"phone_number": phone, "execution_id": run_id},
        outputs={"result": "历史结果"},
    ))


def traces(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines()]


async def test_export_includes_full_subtree_and_historical_definition(trace_env):
    client, service = trace_env
    store = service.store
    version = await store.ensure_definition_version(
        "invitation_strategy", {"historical": "definition"},
        template_hashes={"script": "historical-prompt-hash"}, code_commit="old-commit",
    )
    await add_run(store, "root", version=version)
    await add_run(store, "audio", parent="root", workflow="invitation_outbound_audio")
    await add_run(store, "asr", parent="audio", workflow="invitation_audio_content_extract")
    await add_run(store, "unrelated")
    await store.upsert_node_execution(
        run_id="root", node_id="script", node_title="话术", node_type="llm",
        call_path="", status="succeeded", inputs={"profile": "原始画像"},
        outputs={"text": "历史话术"}, attempt_count=2,
    )
    for attempt_no in (1, 2):
        await store.insert_attempt(
            run_id="root", node_id="script", call_path="", attempt_no=attempt_no,
            kind="llm", request={"messages": [{"role": "user", "content": "实际提示词"}]},
            response_text="实际模型响应", provider_request_id=f"provider-{attempt_no}",
        )
    await store.insert_attempt(
        run_id="asr", node_id="recognize", call_path="files[0]", attempt_no=1,
        kind="http", request={"headers": {"X-API-Key": "***REDACTED***"}},
        response_text="实际 ASR 响应",
    )
    before = await store.get_run("root")
    response = await client.get("/traces/export", params={"run_id": "root"})
    [trace] = traces(response)
    assert response.headers["X-Trace-Count"] == "1"
    assert "attachment" in response.headers["Content-Disposition"]
    assert trace["root_run_id"] == "root" and trace["captured_at_ms"] > 0
    assert set(trace["runs"]) == {"root", "audio", "asr"}
    root = trace["runs"]["root"]
    assert root["inputs"]["execution_id"] == "root"
    assert root["outputs"] == {"result": "历史结果"}
    assert root["child_run_ids"] == ["audio"]
    assert trace["runs"]["audio"]["child_run_ids"] == ["asr"]
    assert root["nodes"][0]["inputs_json"] == {"profile": "原始画像"}
    assert [a["attempt_no"] for a in root["attempts"]] == [1, 2]
    assert root["attempts"][0]["request_json"]["messages"][0]["content"] == "实际提示词"
    assert trace["runs"]["asr"]["attempts"][0]["response_text"] == "实际 ASR 响应"
    assert "***REDACTED***" in response.text
    definition = trace["definitions"][str(version)]
    assert definition["code_commit"] == "old-commit"
    assert definition["definition"] == {"historical": "definition"}
    assert definition["template_hashes_json"] == {"script": "historical-prompt-hash"}
    assert await store.get_run("root") == before


async def test_range_filters_roots_but_keeps_all_descendants(trace_env):
    client, service = trace_env
    store = service.store
    await add_run(store, "selected", created=100, status="failed")
    await add_run(store, "after-range", created=101, status="failed")
    await add_run(store, "profile", created=100, status="failed", workflow="customer_profile_pipeline")
    await add_run(store, "other-phone", created=100, status="failed", phone="13900000000")
    await add_run(store, "other-status", created=100)
    await add_run(store, "child", created=999, status="succeeded", parent="selected")
    [trace] = traces(await client.get("/traces/export", params={
        "workflow_id": "invitation_strategy", "business_ref": "13800000000",
        "status": "failed", "created_after_ms": 100, "created_before_ms": 100,
    }))
    assert trace["root_run_id"] == "selected"
    assert set(trace["runs"]) == {"selected", "child"}


async def test_empty_range_and_unknown_run(trace_env):
    client, _ = trace_env
    response = await client.get("/traces/export", params={"business_ref": "missing"})
    assert traces(response) == []
    assert response.headers["X-Trace-Count"] == "0"
    assert (await client.get("/traces/export", params={"run_id": "missing"})).status_code == 404


async def test_profile_and_explicit_child_traces_use_the_same_api(trace_env):
    client, service = trace_env
    await add_run(service.store, "profile", workflow="customer_profile_pipeline")
    await add_run(service.store, "child", parent="profile", workflow="audio_content_extract")
    [profile] = traces(await client.get("/traces/export", params={
        "workflow_id": "customer_profile_pipeline",
    }))
    assert set(profile["runs"]) == {"profile", "child"}
    [child] = traces(await client.get("/traces/export", params={"run_id": "child"}))
    assert child["root_run_id"] == "child" and set(child["runs"]) == {"child"}


async def test_filter_values_are_not_sql(trace_env):
    client, service = trace_env
    await add_run(service.store, "root")
    assert traces(await client.get("/traces/export", params={
        "business_ref": "' OR 1=1 --",
    })) == []


@pytest.mark.parametrize("params", [
    {"created_after_ms": 2, "created_before_ms": 1},
    {"created_after_ms": -1}, {"created_before_ms": "yesterday"},
    {"created_before_ms": 2**63}, {"run_id": ""}, {"status": "unknown"},
])
async def test_invalid_filters_are_rejected(trace_env, params):
    client, _ = trace_env
    assert (await client.get("/traces/export", params=params)).status_code == 422


async def test_export_is_not_limited_to_500_roots(trace_env):
    client, service = trace_env
    records = [(f"r-{n:04}", "invitation_strategy", "queued", n + 1) for n in range(601)]

    def seed(connection):
        connection.executemany(
            "INSERT INTO workflow_runs(run_id,workflow_id,status,created_at_ms) VALUES(?,?,?,?)",
            records,
        )
        connection.commit()

    await service.store._run(seed)
    response = await client.get("/traces/export")
    exported = traces(response)
    assert response.headers["X-Trace-Count"] == "601"
    assert [t["root_run_id"] for t in exported] == [r[0] for r in records]


async def test_membership_is_fixed_before_streaming(trace_env, monkeypatch):
    client, service = trace_env
    await add_run(service.store, "first")
    original = service.store.execution_trace

    async def read_then_insert(run_id):
        trace = await original(run_id)
        await add_run(service.store, "arrived-during-export")
        return trace

    monkeypatch.setattr(service.store, "execution_trace", read_then_insert)
    response = await client.get("/traces/export")
    assert [t["root_run_id"] for t in traces(response)] == ["first"]
    assert response.headers["X-Trace-Count"] == "1"


async def test_trace_uses_one_database_snapshot(trace_env, tmp_path):
    _, service = trace_env
    store = service.store
    await add_run(store, "root", status="running")
    await store.upsert_node_execution(
        run_id="root", node_id="n", node_title="节点", node_type="code",
        call_path="", status="running", outputs={"value": "before"},
    )
    changed = False

    def concurrent_write(sql):
        nonlocal changed
        if changed or "FROM node_executions" not in sql:
            return
        changed = True
        with sqlite3.connect(tmp_path / "test.db") as other:
            other.execute("UPDATE workflow_runs SET status='succeeded' WHERE run_id='root'")
            other.execute("UPDATE node_executions SET outputs_json=?", ('{"value":"after"}',))

    await store._run(lambda c: c.set_trace_callback(concurrent_write))
    try:
        trace = await store.execution_trace("root")
    finally:
        await store._run(lambda c: c.set_trace_callback(None))
    assert changed
    assert trace["runs"]["root"]["status"] == "running"
    assert trace["runs"]["root"]["nodes"][0]["outputs_json"] == {"value": "before"}
    assert (await store.get_run("root"))["status"] == "succeeded"


async def test_cancelled_read_does_not_kill_database_thread(trace_env):
    _, service = trace_env
    store = service.store
    await add_run(store, "root")
    entered, release = threading.Event(), threading.Event()

    def pause(sql):
        if "FROM node_executions" in sql:
            entered.set()
            assert release.wait(5)

    await store._run(lambda c: c.set_trace_callback(pause))
    task = asyncio.create_task(store.execution_trace("root"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.wait_for(store._run(lambda c: c.set_trace_callback(None)), 5)
    assert (await asyncio.wait_for(store.get_run("root"), 5))["run_id"] == "root"


async def test_existing_read_routes_remain_available_and_no_store_returns_503(trace_env, monkeypatch):
    client, service = trace_env
    await add_run(service.store, "root")
    assert (await client.get("/runs/root")).status_code == 200
    assert (await client.get("/runs?workflow_id=invitation_strategy")).json()[0]["run_id"] == "root"
    with monkeypatch.context() as patch:
        patch.setattr(service, "store", None)
        assert (await client.get("/traces/export")).status_code == 503

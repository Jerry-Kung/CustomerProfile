"""最小 API 的测试。

用 ``httpx.ASGITransport`` 直接打 ASGI 应用，不启真实端口。服务用固定响应装配，
不出网、不写生产库。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from customer_profile.api import create_app
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.workflows import human_corrected_info as hci
from customer_profile.workflows import mengshi_it_system_data as mengshi
from customer_profile.workflows import synthetic_fanout as syn

from .conftest import make_settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"


@pytest.fixture
async def client(tmp_path):
    async def factory():
        settings = make_settings(tmp_path)
        replay = ReplaySource.from_file(FIXTURE, strict=True)
        return await build_service(settings, replay=replay)

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        # 触发 lifespan，装配服务
        async with app.router.lifespan_context(app):
            yield http


# ---------------------------------------------------------------- 测试辅助

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "skipped", "interrupted"}


async def _wait_terminal(client, run_id: str, timeout: float = 30.0) -> dict:
    """轮询到运行进入终态再返回详情。

    ``POST /runs`` 是异步语义（先落库即返回），调用方拿到 run_id 时运行通常还在跑。
    断言最终状态前必须先等它结束——直接断言会把「还没跑完」误判成「结果不对」。
    """
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = (await client.get(f"/runs/{run_id}")).json()
        if detail.get("status") in TERMINAL_STATUSES:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"运行 {run_id} 在 {timeout}s 内未进入终态，最后一次状态：{detail.get('status')}"
    )


# ---------------------------------------------------------------- 元信息


async def test_health_reports_replay_mode(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["replay_mode"] == "fixture"


async def test_health_lists_available_workflows(client):
    body = (await client.get("/health")).json()
    # 子工作流也必须是完整定义并出现在清单里（主流程会引用它）
    assert set(body["workflows"]) == {
        mengshi.WORKFLOW_ID,
        hci.WORKFLOW_ID,
        syn.WORKFLOW_ID,
        syn.CHILD_WORKFLOW_ID,
    }


async def test_workflow_list_has_counts(client):
    rows = (await client.get("/workflows")).json()
    by_id = {row["workflow_id"]: row for row in rows}
    assert by_id[mengshi.WORKFLOW_ID]["node_count"] == 4
    assert by_id[hci.WORKFLOW_ID]["node_count"] == 4


async def test_topology_comes_from_the_same_definition(client):
    """前端拓扑与调度器取自同一份定义，不存在第二张图。"""
    topology = (await client.get(f"/workflows/{hci.WORKFLOW_ID}/topology")).json()
    assert topology["workflow_id"] == hci.WORKFLOW_ID
    assert {n["node_id"] for n in topology["nodes"]} == {
        node.node_id for node in hci.WORKFLOW.nodes
    }
    assert len(topology["edges"]) == len(hci.WORKFLOW.edge_list())


async def test_topology_404_for_unknown_workflow(client):
    assert (await client.get("/workflows/nope/topology")).status_code == 404


# ---------------------------------------------------------------- 提交与查询


async def test_submit_returns_run_id(client):
    response = await client.post(
        "/runs",
        json={
            "workflow_id": hci.WORKFLOW_ID,
            "inputs": {"phone_number": "13800000001"},
            "business_ref": "13800000001",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["workflow_id"] == hci.WORKFLOW_ID


async def test_submitted_run_is_queryable_by_run_id(client):
    """任务先落库再返回 run_id，因此拿到 ID 就能查到。"""
    run_id = (
        await client.post(
            "/runs",
            json={"workflow_id": hci.WORKFLOW_ID, "inputs": {"phone_number": "13800000001"}},
        )
    ).json()["run_id"]

    detail = await _wait_terminal(client, run_id)
    assert detail["run_id"] == run_id
    assert detail["workflow_id"] == hci.WORKFLOW_ID
    assert detail["nodes"], "运行详情应带节点明细"


async def test_submit_unknown_workflow_is_404(client):
    response = await client.post(
        "/runs", json={"workflow_id": "nope", "inputs": {}}
    )
    assert response.status_code == 404


async def test_detail_404_for_unknown_run(client):
    assert (await client.get("/runs/deadbeef")).status_code == 404


async def test_mengshi_run_completes_over_api(client):
    run_id = (
        await client.post(
            "/runs",
            json={
                "workflow_id": mengshi.WORKFLOW_ID,
                "inputs": mengshi.default_inputs("[]"),
            },
        )
    ).json()["run_id"]

    detail = await _wait_terminal(client, run_id)
    assert detail["status"] == "succeeded", detail.get("error")
    assert detail["outputs"]["IT_customer_info"] == (
        "客户留存在销售数据系统中的信息记录为空。"
    )


async def test_child_run_appears_in_detail(client):
    """子流程调用留下的子运行可在详情里看到 ID（V0.4 据此下钻）。"""
    run_id = (
        await client.post(
            "/runs",
            json={
                "workflow_id": syn.WORKFLOW_ID,
                "inputs": syn.default_inputs(),
            },
        )
    ).json()["run_id"]

    detail = await _wait_terminal(client, run_id)
    assert detail["child_run_ids"], "子流程调用没有产生子运行"

    child = (await client.get(f"/runs/{detail['child_run_ids'][0]}")).json()
    assert child["workflow_id"] == syn.CHILD_WORKFLOW_ID
    assert child["nodes"]


async def test_attempts_are_visible_in_detail(client):
    run_id = (
        await client.post(
            "/runs",
            json={"workflow_id": hci.WORKFLOW_ID, "inputs": {"phone_number": "13800000001"}},
        )
    ).json()["run_id"]

    detail = await _wait_terminal(client, run_id)
    assert detail["attempts"], "运行详情应带请求尝试层记录"
    assert {a["kind"] for a in detail["attempts"]} == {"http"}


# ---------------------------------------------------------------- 列表


async def test_list_excludes_child_runs_by_default(client):
    run_id = (
        await client.post(
            "/runs", json={"workflow_id": syn.WORKFLOW_ID, "inputs": syn.default_inputs()}
        )
    ).json()["run_id"]
    await _wait_terminal(client, run_id)

    rows = (await client.get("/runs")).json()
    workflow_ids = {row["workflow_id"] for row in rows}
    assert syn.CHILD_WORKFLOW_ID not in workflow_ids


async def test_list_can_include_child_runs(client):
    run_id = (
        await client.post(
            "/runs", json={"workflow_id": syn.WORKFLOW_ID, "inputs": syn.default_inputs()}
        )
    ).json()["run_id"]
    await _wait_terminal(client, run_id)

    rows = (await client.get("/runs", params={"include_children": True})).json()
    assert any(row["workflow_id"] == syn.CHILD_WORKFLOW_ID for row in rows)


async def test_list_filters_by_business_ref(client):
    run_id = (
        await client.post(
            "/runs",
            json={
                "workflow_id": hci.WORKFLOW_ID,
                "inputs": {"phone_number": "13800000001"},
                "business_ref": "13800000001",
            },
        )
    ).json()["run_id"]
    await _wait_terminal(client, run_id)

    rows = (await client.get("/runs", params={"business_ref": "13800000001"})).json()
    assert rows and all(row["business_ref"] == "13800000001" for row in rows)


async def test_list_filters_by_workflow_id(client):
    run_id = (
        await client.post(
            "/runs",
            json={"workflow_id": mengshi.WORKFLOW_ID, "inputs": mengshi.default_inputs("[]")},
        )
    ).json()["run_id"]
    await _wait_terminal(client, run_id)

    rows = (await client.get("/runs", params={"workflow_id": mengshi.WORKFLOW_ID})).json()
    assert rows and all(row["workflow_id"] == mengshi.WORKFLOW_ID for row in rows)


async def test_list_rejects_oversized_limit(client):
    assert (await client.get("/runs", params={"limit": 10_000})).status_code == 422

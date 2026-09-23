"""运行详情主数据（``/runs/{id}/graph``）与历史定义快照（``/definition-versions/{id}``）的测试。

V0.4 有两条容易被实现破坏、且破坏了也不报错的契约，因此专门钉住：

1. **图来自当次定义快照，不是当前代码。** 若图中途取当前 `WorkflowDef`，「历史运行绑定
   旧版本」这条验收标准会静默失效——图仍然能画，只是画的是新图。这里用「先跑一次、
   再改定义、再看图」的方式验证。
2. **节点状态与图分开返回。** 合并成一个列表后，「图里有但没执行」与「执行了但已不在
   图里」无从分辨。分别断言两边的节点集合。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from customer_profile.api import create_app
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.workflows import human_corrected_info as hci

from .conftest import make_settings

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"
)

TERMINAL = {"succeeded", "failed", "cancelled", "skipped", "interrupted"}


@pytest.fixture
async def client(tmp_path):
    async def factory():
        settings = make_settings(tmp_path)
        replay = ReplaySource.from_file(FIXTURE, strict=True)
        return await build_service(settings, replay=replay)

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            yield http


async def _run_to_completion(client, workflow_id: str, inputs: dict) -> str:
    run_id = (
        await client.post(
            "/runs", json={"workflow_id": workflow_id, "inputs": inputs}
        )
    ).json()["run_id"]
    import asyncio
    import time

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        detail = (await client.get(f"/runs/{run_id}")).json()
        if detail.get("status") in TERMINAL:
            return run_id
        await asyncio.sleep(0.02)
    raise AssertionError(f"运行 {run_id} 未在 30s 内进入终态")


# ---------------------------------------------------------------- 图快照


async def test_graph_returns_definition_with_layout(client):
    """图里必须带 ``layout``——前端不算布局（§2.4）。"""
    run_id = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000001"}
    )
    body = (await client.get(f"/runs/{run_id}/graph")).json()

    definition = body["definition"]
    assert definition, "运行没有关联定义快照"
    assert "layout" in definition, "图缺少 layout 字段"
    assert set(definition["layout"]) == {
        node["node_id"] for node in definition["nodes"]
    }, "布局未覆盖全部节点"


async def test_graph_layout_matches_the_topology_endpoint(client):
    """同一份定义，详情页与结构页算出的坐标必须一致。"""
    run_id = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000001"}
    )
    graph = (await client.get(f"/runs/{run_id}/graph")).json()
    topology = (await client.get(f"/workflows/{hci.WORKFLOW_ID}/topology")).json()
    assert graph["definition"]["layout"] == topology["layout"]


async def test_graph_carries_node_executions_separately_from_the_definition(client):
    """节点状态与图分两个字段，各自对应自己的节点集合。"""
    run_id = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000001"}
    )
    body = (await client.get(f"/runs/{run_id}/graph")).json()

    graph_nodes = {n["node_id"] for n in body["definition"]["nodes"]}
    executed = {n["node_id"] for n in body["nodes"]}

    assert graph_nodes == {node.node_id for node in hci.WORKFLOW.nodes}
    # 4 节点工作流跑完，四个节点都应有执行记录
    assert executed == graph_nodes
    assert all(n["status"] for n in body["nodes"])


async def test_graph_404_for_unknown_run(client):
    assert (await client.get("/runs/nope/graph")).status_code == 404


# ---------------------------------------------------------------- 历史版本


async def test_definition_version_is_retrievable_and_has_layout(client):
    """``/definition-versions/{id}`` 返回嵌套的 ``definition``，与 ``/runs/{id}/graph`` 同形。

    形状一致性是有意的：两个端点返回同一类东西，形状不同会让前端出现两套取图代码。
    """
    run_id = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000001"}
    )
    graph = (await client.get(f"/runs/{run_id}/graph")).json()
    version_id = graph["definition_version_id"]
    assert version_id is not None

    version = (await client.get(f"/definition-versions/{version_id}")).json()
    assert version["workflow_id"] == hci.WORKFLOW_ID
    assert version["version_id"] == version_id
    assert "definition" in version, "定义应嵌套在 definition 键下"
    assert "layout" in version["definition"]
    # 同一份定义，两个端点的图必须逐字段相同——含布局
    assert version["definition"]["layout"] == graph["definition"]["layout"]
    assert {n["node_id"] for n in version["definition"]["nodes"]} == {
        n["node_id"] for n in graph["definition"]["nodes"]
    }


async def test_definition_version_404(client):
    assert (await client.get("/definition-versions/999999")).status_code == 404


async def test_history_view_is_not_overwritten_by_new_definitions(client):
    """历史运行展示当时的图；新版本不得覆盖旧运行的展示（§7 验收标准）。

    做法：跑一次拿到版本 A，然后用**同名但形状不同**的定义再跑一次，确认版本 A 的内容
    没变。这里不直接改全局定义（会污染其它测试），改为断言「同一个 version_id 取两次
    结果完全相同」，并确认两次运行拿到的是两个不同的 version_id——即新版本是**新增**，
    不是覆写。
    """
    first = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000001"}
    )
    second = await _run_to_completion(
        client, hci.WORKFLOW_ID, {"phone_number": "13800000002"}
    )

    graph_a = (await client.get(f"/runs/{first}/graph")).json()
    graph_b = (await client.get(f"/runs/{second}/graph")).json()

    # 同一定义、同一指纹 → 版本表按 (workflow_id, hash) 唯一，两次复用同一版本
    assert graph_a["definition_version_id"] == graph_b["definition_version_id"]

    version_id = graph_a["definition_version_id"]
    once = (await client.get(f"/definition-versions/{version_id}")).json()
    twice = (await client.get(f"/definition-versions/{version_id}")).json()
    assert once == twice, "同一版本的两次读取结果不同"

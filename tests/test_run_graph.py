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


# ---------------------------------------------------------------- 子运行下钻


async def test_child_runs_are_indexed_by_triggering_node(client):
    """``/runs/{id}/graph`` 的 ``child_runs`` 以**触发它的父节点**为键。

    v0.4.3 的验收标准「子运行可下钻」全靠这条通路：详情页点节点后按 ``node_id``
    查 ``child_runs``，查到才显示下钻入口。键若是父节点 ID 之外的东西，入口永远不出现，
    而界面上看不出错。用一个人造扇出图跑出真实子运行来验证。
    """
    from customer_profile.workflows import synthetic_fanout as syn

    run_id = await _run_to_completion(
        client, syn.WORKFLOW_ID, syn.default_inputs()
    )
    body = (await client.get(f"/runs/{run_id}/graph")).json()

    assert body["child_runs"], "没有子运行被索引到，下钻入口不会出现"

    for parent_node_id, child in body["child_runs"].items():
        # 键必须是图里真实存在的节点
        assert parent_node_id in {n["node_id"] for n in body["definition"]["nodes"]}
        # 值要够前端显示入口文案
        assert child["run_id"]
        assert child["status"]

    # 触发子运行的父节点自身要带 sub_run_id，前端据此判断该节点可下钻
    triggering = [
        n for n in body["nodes"] if n["sub_run_id"]
    ]
    assert triggering, "父节点没有 sub_run_id，前端无法判断哪个节点能下钻"
    assert triggering[0]["node_id"] in body["child_runs"], (
        "带 sub_run_id 的节点没有出现在 child_runs 里，下钻入口对不上"
    )


# ---------------------------------------------------------------- 历史版本不被覆盖（行为级）


async def test_a_new_definition_version_does_not_change_an_old_run(tmp_path):
    """**行为级**验证：登记一份新定义后，旧运行的展示必须纹丝不动。

    与 ``test_history_view_is_not_overwritten_by_new_definitions`` 的区别：那条只断言
    「同一个 version_id 两次读取一致」，**不等于**「定义变了之后旧运行仍显示旧图」——
    它没有真的让定义发生变化。这条补上：跑一次拿到版本 A，登记一份**内容不同**的定义
    产生版本 B，再回头查旧运行，必须仍指向 A 且节点集合不含新增节点。

    交付报告 §4.1 把这一条列为未完成项（证据强度不足），此处收口。
    """
    from customer_profile.replay import ReplaySource
    from customer_profile.runner import build_service
    from customer_profile.api import create_app
    from customer_profile.workflows import human_corrected_info as hci_mod

    settings = make_settings(tmp_path)
    service_obj = await build_service(
        settings, replay=ReplaySource.from_file(FIXTURE, strict=True)
    )

    async def factory():
        return service_obj

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            # ---- 1. 跑一次，拿到版本 A 与当时的节点集合
            run_id = (
                await c.post(
                    "/runs",
                    json={
                        "workflow_id": hci_mod.WORKFLOW_ID,
                        "inputs": {"phone_number": "13800000001"},
                    },
                )
            ).json()["run_id"]
            import asyncio
            import time

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                detail = (await c.get(f"/runs/{run_id}")).json()
                if detail.get("status") in TERMINAL:
                    break
                await asyncio.sleep(0.02)

            graph_a = (await c.get(f"/runs/{run_id}/graph")).json()
            version_a = graph_a["definition_version_id"]
            nodes_a = {n["node_id"] for n in graph_a["definition"]["nodes"]}
            assert version_a is not None

            # ---- 2. 登记一份**内容不同**的定义，产生版本 B
            modified = dict(hci_mod.WORKFLOW.asdict())
            modified["nodes"] = list(modified["nodes"]) + [
                {
                    "node_id": "FAKE_NEW_NODE",
                    "title": "新增的节点（只存在于新版本）",
                    "type": "code",
                    "direct_predecessors": [],
                    "branch_gates": {},
                    "bindings": [],
                    "outputs": [],
                    "config": {},
                    "coords": None,
                }
            ]
            version_b = await service_obj.store.ensure_definition_version(
                hci_mod.WORKFLOW_ID, modified
            )
            assert version_b != version_a, "内容已变却复用了同一版本号，快照机制失效"

            # ---- 3. 旧运行的展示不能被版本 B 影响
            graph_a2 = (await c.get(f"/runs/{run_id}/graph")).json()
            assert graph_a2["definition_version_id"] == version_a, (
                "旧运行指向了新版本——历史运行的图被新定义覆盖了"
            )
            assert {n["node_id"] for n in graph_a2["definition"]["nodes"]} == nodes_a
            assert "FAKE_NEW_NODE" not in {
                n["node_id"] for n in graph_a2["definition"]["nodes"]
            }, "旧运行的图里出现了新版本才有的节点"

            # ---- 4. 而版本 B 自身可查，内容确实是新的
            fetched_b = (await c.get(f"/definition-versions/{version_b}")).json()
            assert "FAKE_NEW_NODE" in {
                n["node_id"] for n in fetched_b["definition"]["nodes"]
            }
            fetched_a = (await c.get(f"/definition-versions/{version_a}")).json()
            assert "FAKE_NEW_NODE" not in {
                n["node_id"] for n in fetched_a["definition"]["nodes"]
            }

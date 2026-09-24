"""中断处理与人工出口（V0.5.4）。

一条容易做错的地方：`abandon` 是「结掉一条**已经停掉**的运行」，不是「停掉一条正在
跑的运行」。两者混用会出现「我点了取消但任务还在写外部接口」这种最难查的状态——
因为界面上那条运行已经显示为终态，而进程里它还在跑。

另有两条验收要求在本文件里被钉住：

- 中断运行**保留已完成节点的结果**（`Dify迁移任务说明.md` §7.3：进程中断不能证明
  外部副作用未发生，故不自动续跑，但已完成的部分不能丢）；
- `interrupted` 只由**残留的 running**产生，`queued` 不在此列（差异 W24）——后者是
  持久化队列里等待被领取的任务。
"""

from __future__ import annotations

import httpx
import pytest

from customer_profile.api import create_app
from customer_profile.definitions import RunStatus
from customer_profile.persistence import Store
from customer_profile.persistence.schema import RunRecordRow

from .conftest import make_settings


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "interrupt.db")
    store.connect()
    yield store
    store.close()


# ---------------------------------------------------------------- store 层


async def test_abandon_only_accepts_interrupted(store):
    """只接受 interrupted；其它状态一律不动。

    特别是 running：拿 abandon 去停一条正在跑的运行会与执行中的进程冲突，而界面上
    看不到任何在跑的东西——它已经显示为终态了。
    """
    await store.insert_run(
        RunRecordRow(run_id="live", workflow_id="wf", status=RunStatus.RUNNING)
    )
    assert await store.abandon_run("live") is False
    assert (await store.get_run("live"))["status"] == RunStatus.RUNNING

    await store.insert_run(
        RunRecordRow(run_id="ok", workflow_id="wf", status=RunStatus.SUCCEEDED)
    )
    assert await store.abandon_run("ok") is False

    await store.insert_run(
        RunRecordRow(run_id="stuck", workflow_id="wf", status=RunStatus.INTERRUPTED)
    )
    assert await store.abandon_run("stuck") is True
    assert (await store.get_run("stuck"))["status"] == RunStatus.CANCELLED


async def test_abandon_is_idempotent_in_the_safe_direction(store):
    """重复终结不会二次生效——第二次返回 False，状态保持 cancelled。"""
    await store.insert_run(
        RunRecordRow(run_id="stuck", workflow_id="wf", status=RunStatus.INTERRUPTED)
    )
    assert await store.abandon_run("stuck") is True
    assert await store.abandon_run("stuck") is False
    assert (await store.get_run("stuck"))["status"] == RunStatus.CANCELLED


async def test_abandon_records_when_and_why(store):
    """终结要留下时刻与原因，否则列表里看不出这条是被人工结掉的。"""
    await store.insert_run(
        RunRecordRow(run_id="stuck", workflow_id="wf", status=RunStatus.INTERRUPTED)
    )
    await store.abandon_run("stuck")
    row = await store.get_run("stuck")
    assert row["finished_at_ms"] is not None, "终结时刻未记录"
    assert "人工确认后终结" in (row["error"] or "")


async def test_abandon_preserves_the_interrupt_reason(store):
    """原本的中断原因不能被覆盖——它才是「为什么停下」的答案。"""
    await store.insert_run(
        RunRecordRow(
            run_id="stuck",
            workflow_id="wf",
            status=RunStatus.INTERRUPTED,
            error="进程中断：运行中任务未完成，已完成结果已保留",
        )
    )
    await store.abandon_run("stuck")
    row = await store.get_run("stuck")
    assert "进程中断" in (row["error"] or ""), "中断原因被覆盖了"
    assert "人工确认后终结" in (row["error"] or "")


async def test_interrupted_run_keeps_completed_node_results(store):
    """中断运行保留已完成节点的结果。

    这是「交人工处理」的前提：人工要看的就是「已经跑到哪一步了」。
    """
    await store.insert_run(
        RunRecordRow(run_id="half", workflow_id="wf", status=RunStatus.RUNNING)
    )
    await store.upsert_node_execution(
        run_id="half",
        node_id="n1",
        node_title="已完成节点",
        node_type="code",
        call_path="",
        status="succeeded",
        outputs={"value": "kept"},
    )
    await store.upsert_node_execution(
        run_id="half",
        node_id="n2",
        node_title="被中断的节点",
        node_type="code",
        call_path="",
        status="running",
    )

    # 模拟进程重启时的标记
    await store.mark_running_as_interrupted()

    row = await store.get_run("half")
    assert row["status"] == RunStatus.INTERRUPTED

    nodes = {n["node_id"]: n for n in await store.list_node_executions("half")}
    assert nodes["n1"]["status"] == "succeeded"
    assert nodes["n1"]["outputs_json"] == {"value": "kept"}, "已完成节点的结果被丢了"
    assert nodes["n2"]["status"] == "running", "节点状态不该被运行级标记改写"


# ---------------------------------------------------------------- API 层


async def _client_for(tmp_path):
    """给 API 层用的客户端。

    **必须让服务自己连库**，不能拿一个已在别处的 Store 塞进去：服务的 lifespan 退出时
    会关闭它所持有的 store，而「手工塞进去的那个」的连接是另一个线程独享的——症状是
    运行期报「Store 尚未 connect()」，且看不出是谁关掉了它。
    因此这里按同一路径重建一个服务，与断言用的 store 指向同一个文件。
    """
    from customer_profile.runner import build_service

    async def factory():
        # 与 store fixture 用同一个文件路径：断言读的是库里的最终状态。
        settings = make_settings(tmp_path, database_url=f"sqlite:///{tmp_path / 'interrupt.db'}")
        return await build_service(settings)

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_abandon_endpoint_returns_409_for_running_run(store, tmp_path):
    """running 的运行不能人工终结，返回 409 而不是静默成功。"""
    await store.insert_run(
        RunRecordRow(run_id="live", workflow_id="wf", status=RunStatus.RUNNING)
    )
    app, client = await _client_for(tmp_path)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post("/runs/live/abandon")
        assert response.status_code == 409, response.text
        assert (await store.get_run("live"))["status"] == RunStatus.RUNNING
    finally:
        await client.aclose()


async def test_abandon_endpoint_ends_an_interrupted_run(store, tmp_path):
    await store.insert_run(
        RunRecordRow(run_id="stuck", workflow_id="wf", status=RunStatus.INTERRUPTED)
    )
    app, client = await _client_for(tmp_path)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post("/runs/stuck/abandon")
        assert response.status_code == 200, response.text
        assert response.json()["abandoned"] is True
        assert (await store.get_run("stuck"))["status"] == RunStatus.CANCELLED
    finally:
        await client.aclose()


async def test_abandon_endpoint_404_for_unknown_run(store, tmp_path):
    app, client = await _client_for(tmp_path)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post("/runs/deadbeef/abandon")
        assert response.status_code == 404
    finally:
        await client.aclose()

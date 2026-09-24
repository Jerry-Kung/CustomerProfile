"""提交闸门、入参校验与运行态端点（v0.5.1）。

三件事各有一条容易「看起来正常」的失效方式，专门钉住：

1. **并发上限只写在配置里。** ``MAX_ACTIVE_RUNS`` 在 v0.5.1 之前**没有任何读取方**——
   声明了、文档写了，而运行时完全不生效。这里用「上限为 1 时第二次提交必须被拒」来证明它
   真的接上了，而不是只断言配置项存在。
   **V0.5.2 起该闸门只作用于进程内路径**（``Service.submit_local``）：队列模型下
   ``Service.submit`` 只入队、执行由 worker 负责，API 侧改为按队列深度拒绝（差异 W23）。
   因此下面这些用例走 ``submit_local``。
2. **被拒的提交留下半条记录。** 若先落库再判名额，拒绝就会在库里留下一个永远不跑的运行——
   列表上看得见、状态永远 queued。因此要断言被拒后**记录数不变**。
3. **手机号校验在执行期才报错。** 校验若只靠 ``start`` 节点，少传或传错会变成一个**失败的运行**
   而不是一个可解释的 4xx，调用方拿到的错误里看不到「入参不对」。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from customer_profile.api import ENTRY_WORKFLOW_ID, create_app
from customer_profile.replay import ReplaySource
from customer_profile.runner import RunSlotGate, RunSlotUnavailable, build_service

from .conftest import make_settings

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"
)


async def _make_service(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return await build_service(
        settings, replay=ReplaySource.from_file(FIXTURE, strict=True)
    )


def _count_runs_in_db(tmp_path) -> int:
    """直接查 SQLite 文件里的运行条数。

    不走 ``service.store``：应用的 ``lifespan`` 退出时会关掉 store，于是「断言被拒的提交
    没留下记录」这件事在 ``async with`` 之外**无法**用 store 查——它已经关了。直接查库
    同时也绕开了「用被测对象验证自己」的问题。
    """
    import sqlite3

    db = tmp_path / "test.db"
    if not db.is_file():
        return 0
    connection = sqlite3.connect(db)
    try:
        row = connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


async def _client_for(service):
    async def factory():
        return service

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------- 闸门本身

def test_gate_accepts_up_to_the_limit():
    gate = RunSlotGate(2)
    gate.acquire()
    assert (gate.active, gate.available) == (1, 1)
    gate.acquire()
    assert (gate.active, gate.available) == (2, 0)


def test_gate_refuses_beyond_the_limit():
    gate = RunSlotGate(1)
    gate.acquire()
    with pytest.raises(RunSlotUnavailable) as excinfo:
        gate.acquire()
    # 错误信息要能直接回答「为什么被拒」，而不只是抛一个类型
    assert excinfo.value.limit == 1
    assert excinfo.value.active == 1


def test_gate_release_frees_a_slot():
    gate = RunSlotGate(1)
    gate.acquire()
    gate.release()
    gate.acquire()  # 不抛即通过
    assert gate.active == 1


def test_gate_ignores_extra_releases():
    """多释放不得让计数器变负——那会让闸门**永久多放行**若干个运行。"""
    gate = RunSlotGate(1)
    gate.release()
    gate.release()
    assert gate.active == 0
    gate.acquire()
    assert gate.active == 1


def test_gate_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        RunSlotGate(0)


# ---------------------------------------------------------------- 闸门接在服务上

async def test_submit_is_refused_when_slots_are_exhausted(tmp_path):
    """上限用满时提交被拒，且**不留半条运行记录**。"""
    service = await _make_service(tmp_path, max_active_runs=1)
    try:
        assert service.run_slots is not None, "MAX_ACTIVE_RUNS 没有被接上"

        # 占掉唯一的名额，模拟「已有一个运行在跑」。
        service.run_slots.acquire()
        assert service.run_slots.active == 1

        # 用 submit_local 而非 submit：V0.5.2 起后者只入队、不过名额闸门，
        # 名额限制作用于**进程内执行路径**（差异 W23）。
        with pytest.raises(RunSlotUnavailable):
            await service.submit_local(
                ENTRY_WORKFLOW_ID, {"phone_number": "13800000001"}
            )

        assert _count_runs_in_db(tmp_path) == 0, (
            "被拒的提交留下了运行记录——列表上会出现一个永远不跑的运行"
        )
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_releasing_a_slot_lets_the_next_submit_through(tmp_path):
    """名额释放后下一次提交必须能过——否则闸门会永久卡死。"""
    service = await _make_service(tmp_path, max_active_runs=1)
    try:
        assert service.run_slots is not None
        service.run_slots.acquire()
        service.run_slots.release()
        assert service.run_slots.active == 0

        run_id = await service.submit_local(
            ENTRY_WORKFLOW_ID, {"phone_number": "13800000001"}
        )
        assert run_id
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


def test_gate_is_absent_when_the_limit_is_unset(tmp_path):
    """``MAX_ACTIVE_RUNS=`` 空串按「未设置」处理，此时不装闸门（保持既有行为）。"""
    import asyncio

    async def build():
        service = await _make_service(tmp_path, max_active_runs=None)
        try:
            assert service.run_slots is None
        finally:
            await service.aclose()
            if service.store is not None:
                service.store.close()

    asyncio.run(build())


# ---------------------------------------------------------------- 手机号校验

async def test_entry_rejects_a_malformed_phone_number(tmp_path):
    service = await _make_service(tmp_path)
    app, client = await _client_for(service)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post(
                    "/runs",
                    json={
                        "workflow_id": ENTRY_WORKFLOW_ID,
                        "inputs": {"phone_number": "12345"},
                    },
                )
        assert response.status_code == 422, response.text
        assert "phone_number" in response.text
        # 被拒的提交不得留下运行记录
        assert _count_runs_in_db(tmp_path) == 0
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_entry_rejects_a_missing_phone_number(tmp_path):
    service = await _make_service(tmp_path)
    app, client = await _client_for(service)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post(
                    "/runs", json={"workflow_id": ENTRY_WORKFLOW_ID, "inputs": {}}
                )
        assert response.status_code == 422
        assert _count_runs_in_db(tmp_path) == 0
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_phone_validation_does_not_constrain_other_workflows(tmp_path):
    """校验只对主入口生效。

    其它工作流（如 ``human_corrected_info``）的入参契约不同，在这里一并约束会把它们的
    合法调用挡掉——那是把入口契约硬编码到了全部工作流上。
    """
    from customer_profile.workflows import human_corrected_info as hci

    service = await _make_service(tmp_path)
    app, client = await _client_for(service)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                response = await http.post(
                    "/runs",
                    json={"workflow_id": hci.WORKFLOW_ID, "inputs": {"phone_number": "x"}},
                )
        assert response.status_code == 200, response.text
        assert response.json()["run_id"]
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


# ---------------------------------------------------------------- 运行态端点

async def test_runtime_reports_config_without_credentials(tmp_path):
    service = await _make_service(tmp_path, max_active_runs=3)
    app, client = await _client_for(service)
    try:
        async with client as http:
            async with app.router.lifespan_context(app):
                body = (await http.get("/runtime")).json()

        assert body["replay_mode"] == "fixture"
        assert body["writeback_enabled"] is False, (
            "回写开关必须如实报出：运行显示 succeeded 时它决定「有没有写进生产库」"
        )
        assert body["max_active_runs"] == 3
        assert body["available_run_slots"] == 3
        assert body["active_runs"] == 0
        assert "llm_model" in body

        # 凭据不得出现在返回值里
        leaked = [
            key
            for key in body
            if key.endswith("_key") or "api_key" in key or "token" in key
        ]
        assert not leaked, f"运行态端点泄漏了凭据字段：{leaked}"
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

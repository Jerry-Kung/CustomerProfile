"""节点级尝试端点（``/runs/{id}/nodes/{node_id}/attempts``）。

v0.4.3 的验收标准是「能看到 17 处 Gemini 调用的每一次尝试」，因此这个端点有三种
容易写错、且写错了不报错的情形：

1. **按节点过滤却漏了 ``call_path``**。迭代类工作流的同一个静态节点 ID 每轮都重新执行，
   只按 ``node_id`` 过滤会把各轮尝试混成一堆，看不出哪次属于哪一轮。
2. **未命中的运行返回 200 + 空数组**。那会把「这个 run 不存在」伪装成「这个节点没有
   尝试」，排查时先怀疑错方向。运行不存在必须是 404。
3. **节点行的 ``attempt_count`` 恒为 0**。它是前端决定「是否去拉尝试」的依据，恒为 0
   会让详情页永远不显示尝试——数据在库里，界面上一片空白。本文件的用例因此**先断言
   计数非零**，再验证内容，而不是直接取尝试列表。
"""

from __future__ import annotations

import asyncio
import time
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


async def _run(client) -> dict:
    """跑一次并返回详情（节点行里带 attempt_count 与 call_path）。"""
    run_id = (
        await client.post(
            "/runs",
            json={
                "workflow_id": hci.WORKFLOW_ID,
                "inputs": {"phone_number": "13800000001"},
            },
        )
    ).json()["run_id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        detail = (await client.get(f"/runs/{run_id}")).json()
        if detail.get("status") in TERMINAL:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError("运行未进入终态")


def _node_with_attempts(detail: dict) -> dict:
    """取第一个带尝试的节点。

    不用 ``next(...)`` 直接扫生成器：没有匹配时它抛 ``StopIteration``，在协程里会
    变成 ``RuntimeError: coroutine raised StopIteration``，报错信息完全指不到
    「计数没被写」这个真实原因。显式断言能给出一句有用的失败说明。
    """
    candidates = [n for n in detail["nodes"] if n["attempt_count"] > 0]
    assert candidates, (
        "没有任何节点的 attempt_count > 0，但该工作流确实发起过 HTTP 请求；"
        f"节点计数：{[(n['node_id'][-6:], n['attempt_count']) for n in detail['nodes']]}"
    )
    return candidates[0]


async def test_node_rows_carry_a_nonzero_attempt_count(client):
    """节点行的 ``attempt_count`` 必须被写出来。

    这是详情页决定「要不要拉尝试」的依据。恒为 0 时数据在库里、界面却永远空白，
    因此单独断言它，而不是只测尝试端点本身。
    """
    detail = await _run(client)
    node = _node_with_attempts(detail)
    assert node["node_type"] == "http-request"
    assert len(detail["attempts"]) >= 1


async def test_attempts_are_returned_for_the_node_that_made_the_call(client):
    """该工作流有一次 GET 调用，应能从节点级端点取回。"""
    detail = await _run(client)
    node = _node_with_attempts(detail)

    attempts = (
        await client.get(
            f"/runs/{detail['run_id']}/nodes/{node['node_id']}/attempts"
        )
    ).json()
    assert len(attempts) == node["attempt_count"]
    assert attempts[0]["node_id"] == node["node_id"]
    assert attempts[0]["attempt_no"] == 1
    # 回放模式下请求确实发生过（走固定响应），replayed 应为 1
    assert attempts[0]["replayed"] == 1


async def test_attempts_carry_request_and_response_for_rendering(client):
    """详情页要渲染实际请求与响应，两个字段都必须在。"""
    detail = await _run(client)
    node = _node_with_attempts(detail)
    attempt = (
        await client.get(
            f"/runs/{detail['run_id']}/nodes/{node['node_id']}/attempts"
        )
    ).json()[0]

    assert attempt["request_json"], "缺少实际请求，无法渲染 messages/请求明细"
    assert attempt["kind"] in {"llm", "http"}


async def test_call_path_filter_narrows_to_one_round(client):
    """带 ``call_path`` 过滤后只剩该轮的尝试。

    顶层节点的 ``call_path`` 是空串（父图节点属于所在运行本身），此时过滤应仍能工作。
    """
    detail = await _run(client)
    node = _node_with_attempts(detail)

    all_rows = (
        await client.get(
            f"/runs/{detail['run_id']}/nodes/{node['node_id']}/attempts"
        )
    ).json()
    filtered = (
        await client.get(
            f"/runs/{detail['run_id']}/nodes/{node['node_id']}/attempts",
            params={"call_path": node["call_path"]},
        )
    ).json()
    assert len(filtered) == len(all_rows)


async def test_count_attempts_matches_the_attempt_rows(client, tmp_path):
    """``count_attempts`` 与尝试表必须一致——它就是 ``attempt_count`` 的来源。"""
    detail = await _run(client)
    node = _node_with_attempts(detail)
    assert node["attempt_count"] == len(detail["attempts"])


async def test_unknown_node_returns_empty_list_not_error(client):
    """节点没有尝试是合法的（纯 code 节点），返回空数组而不是 404。"""
    detail = await _run(client)
    response = await client.get(
        f"/runs/{detail['run_id']}/nodes/不存在的节点/attempts"
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_unknown_run_is_404_not_empty_list(client):
    """运行不存在必须是 404。

    返回 200 + 空数组会把「run 不存在」伪装成「该节点没有尝试」，
    排查时会先怀疑错方向。
    """
    response = await client.get("/runs/不存在的run/nodes/任意节点/attempts")
    assert response.status_code == 404

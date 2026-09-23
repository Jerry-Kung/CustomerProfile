"""节点时间戳与执行顺序（v0.4.4）。

**这是一个真实缺陷的回归测试。** ``started_at_ms`` 曾被写成与 ``duration_ms`` 同一个
表达式（``int((self._clock() - started) * 1000)``），即「距节点开始的耗时」而非时间戳；
更隐蔽的是它**根本没落到库里**——``upsert_node_execution`` 的
``ON CONFLICT ... DO UPDATE SET`` 列表里没有这一列，``node_started`` 插入时留空，
收尾时也不会覆盖它，于是永远是 NULL。

后果是「执行顺序」视图无从绘制：没有墙钟起点就摆不到同一条时间轴上，也无法判断两个
节点是否并发。这里钉住三件事：它是墙钟、它能落库、它足以算出重叠。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from customer_profile.api import create_app
from customer_profile.runner import build_service
from customer_profile.settings import Settings

from .conftest import make_settings

TERMINAL = {"succeeded", "failed", "cancelled", "skipped", "interrupted"}

# 墙钟量级下界：2020-09-13。远大于任何「耗时毫秒数」，足以区分时间戳与耗时。
EPOCH_FLOOR_MS = 1_600_000_000_000


@pytest.fixture
async def client(tmp_path):
    """不走回放：本文件要验证真实并发下的时间关系，固定响应会让扇出退化为顺序。"""

    async def factory():
        settings = make_settings(
            tmp_path,
            replay_mode="off",
            llm_base_url="http://127.0.0.1:1/v1",
            llm_api_key="k",
            llm_model="m",
        )
        return await build_service(settings)

    app = create_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            yield http


async def _run_fanout(client) -> dict:
    """跑人造扇出图：它含一快一慢两个并行分支，用于证明重叠可算。"""
    from customer_profile.workflows import synthetic_fanout as syn

    run_id = (
        await client.post(
            "/runs",
            json={"workflow_id": syn.WORKFLOW_ID, "inputs": syn.default_inputs()},
        )
    ).json()["run_id"]
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        detail = (await client.get(f"/runs/{run_id}")).json()
        if detail.get("status") in TERMINAL:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError("运行未进入终态")


async def test_node_started_at_ms_is_wall_clock_not_a_duration(client):
    """节点起点必须是墙钟时间戳，不能是「距节点开始的耗时」。

    判别方式：墙钟在 2020 年之后就大于 1.6e12，而任何节点的耗时都远小于它。
    旧实现把 ``started_at_ms`` 写成耗时（毫秒级小数），这条断言直接失败。
    """
    detail = await _run_fanout(client)
    assert detail["nodes"], "没有节点执行记录"

    for node in detail["nodes"]:
        assert node["started_at_ms"] is not None, (
            f"节点 {node['node_id']} 的 started_at_ms 没落库（upsert 的 DO UPDATE SET "
            "列表里可能又漏了这一列）"
        )
        assert node["started_at_ms"] > EPOCH_FLOOR_MS, (
            f"节点 {node['node_id']} 的 started_at_ms={node['started_at_ms']} 不像墙钟，"
            "像是耗时"
        )


async def test_node_started_at_ms_is_not_equal_to_duration(client):
    """起点与耗时是两个量。旧实现让它们取自同一个表达式，因此恒等。"""
    detail = await _run_fanout(client)
    equal = [
        n["node_id"]
        for n in detail["nodes"]
        if n["started_at_ms"] is not None and n["started_at_ms"] == n["duration_ms"]
    ]
    assert not equal, f"这些节点的 started_at_ms 与 duration_ms 相同，像是同一表达式：{equal}"


async def test_node_timestamps_sit_inside_the_run_window(client):
    """每个节点的起点应落在「运行起点 ~ 运行起点+运行耗时」的窗口内（留一点余量）。

    这条能抓出「时间戳来自另一个时钟」这类错误——例如混用了单调钟。
    """
    detail = await _run_fanout(client)
    base = detail["started_at_ms"]
    assert base and base > EPOCH_FLOOR_MS
    span = (detail["duration_ms"] or 0) + 5000  # 余量：收尾写入与查询之间有间隔

    for node in detail["nodes"]:
        start = node["started_at_ms"]
        if start is None:
            continue
        assert base <= start <= base + span, (
            f"节点 {node['node_id']} 的起点 {start} 落在运行窗口 "
            f"[{base}, {base + span}] 之外"
        )


async def test_overlapping_nodes_are_detectable(client):
    """时间戳必须足以算出**重叠**——这是「并发不伪装成线性顺序」的前提。

    人造扇出图有一快一慢两个并行分支。若时间戳只记录耗时或只记录结束时间，
    这里算不出任何重叠对，时间线就退化成一条线性列表。
    """
    detail = await _run_fanout(client)
    spans = [
        (n["node_id"], n["started_at_ms"], n["started_at_ms"] + (n["duration_ms"] or 0))
        for n in detail["nodes"]
        if n["started_at_ms"] is not None
    ]
    assert len(spans) >= 4, f"可用于判定的节点太少：{spans}"

    overlaps = [
        (a[0], b[0])
        for index, a in enumerate(spans)
        for b in spans[index + 1 :]
        if a[1] < b[2] and b[1] < a[2]
    ]
    assert overlaps, (
        "没有检测到任何重叠的节点区间。该图含并行分支，算不出重叠说明时间戳不足以"
        "表达并发"
    )


async def test_node_executions_are_ordered_by_start_time(client):
    """节点列表按起点排序——时间线直接按这个顺序渲染。"""
    detail = await _run_fanout(client)
    stamps = [
        n["started_at_ms"] for n in detail["nodes"] if n["started_at_ms"] is not None
    ]
    assert stamps == sorted(stamps), "节点未按起点排序"

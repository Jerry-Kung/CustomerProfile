"""任务列表的筛选、排序与计数。

分页有两类错误是「看起来正常」的，专门钉住：

1. **筛选与计数分叉**。列表按条件过滤、计数不过滤，页数就会比实际多，翻到后半段是
   空页——那看起来像后端丢了数据。用「列表长度与 count 在每套筛选下都自洽」来验证。
2. **排序参数被拼进 SQL**。``order_by`` 来自查询参数，若直接插值就是注入点。用一条
   注入形状的取值验证它会回落到缺省排序，而不是执行。
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


async def _submit_and_wait(client, inputs: dict, business_ref: str | None = None) -> str:
    payload: dict = {"workflow_id": hci.WORKFLOW_ID, "inputs": inputs}
    if business_ref:
        payload["business_ref"] = business_ref
    run_id = (await client.post("/runs", json=payload)).json()["run_id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        detail = (await client.get(f"/runs/{run_id}")).json()
        if detail.get("status") in TERMINAL:
            return run_id
        await asyncio.sleep(0.02)
    raise AssertionError(f"运行 {run_id} 未进入终态")


async def _seed(client, count: int = 3) -> list[str]:
    return [
        await _submit_and_wait(
            client, {"phone_number": f"1380000000{i}"}, business_ref=f"1380000000{i}"
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------- 计数端点


async def test_count_route_is_not_shadowed_by_run_detail(client):
    """``/runs/count`` 必须命中计数端点，而不是被 ``/runs/{run_id}`` 当成 run_id。

    路由按注册顺序匹配，``count`` 若排在 ``{run_id}`` 之后就会被吃掉，
    表现为「查计数返回 404」。
    """
    response = await client.get("/runs/count")
    assert response.status_code == 200
    assert "total" in response.json()


async def test_count_is_zero_when_nothing_ran(client):
    assert (await client.get("/runs/count")).json()["total"] == 0


# ---------------------------------------------------------------- 筛选


async def test_list_and_count_stay_consistent_under_every_filter(client):
    """每套筛选下，列表条数与 count 必须自洽。

    这是分页正确性的核心：两者条件一旦分叉，页数就会与实际数据不符。
    """
    await _seed(client, 3)
    filters: list[dict] = [
        {},
        {"workflow_id": hci.WORKFLOW_ID},
        {"status": "succeeded"},
        {"business_ref": "13800000000"},
        {"workflow_id": hci.WORKFLOW_ID, "status": "succeeded"},
    ]
    for params in filters:
        rows = (await client.get("/runs", params=params)).json()
        total = (await client.get("/runs/count", params=params)).json()["total"]
        assert total == len(rows), f"筛选 {params} 下列表 {len(rows)} 条但计数 {total}"


async def test_filter_by_business_ref_narrows_results(client):
    run_ids = await _seed(client, 3)
    assert len(run_ids) == 3

    rows = (await client.get("/runs", params={"business_ref": "13800000001"})).json()
    assert len(rows) == 1
    assert rows[0]["business_ref"] == "13800000001"


async def test_unknown_status_filter_returns_empty_not_error(client):
    await _seed(client, 2)
    rows = (await client.get("/runs", params={"status": "不存在的状态"})).json()
    assert rows == []
    assert (await client.get("/runs/count", params={"status": "不存在的状态"})).json()[
        "total"
    ] == 0


# ---------------------------------------------------------------- 排序


async def test_order_by_created_desc_is_the_default(client):
    await _seed(client, 3)
    rows = (await client.get("/runs")).json()
    stamps = [row["started_at_ms"] for row in rows]
    assert stamps == sorted(stamps, reverse=True), "缺省应按创建时间倒序"


async def test_order_by_created_asc_reverses(client):
    await _seed(client, 3)
    rows = (await client.get("/runs", params={"order_by": "created_asc"})).json()
    stamps = [row["started_at_ms"] for row in rows]
    assert stamps == sorted(stamps), "created_asc 应按创建时间正序"


async def test_unknown_order_by_falls_back_instead_of_erroring(client):
    """未知排序键回落到缺省，而不是 500。排序参数写错不该让列表打不开。"""
    await _seed(client, 2)
    response = await client.get("/runs", params={"order_by": "没有这个字段"})
    assert response.status_code == 200
    assert len(response.json()) == 2


async def test_order_by_cannot_inject_sql(client):
    """注入形状的排序键必须被白名单挡住。

    若 ``order_by`` 被直接拼进 SQL，这条会以数据库错误暴露；正确行为是安静回落。
    """
    await _seed(client, 2)
    payload = "created_at_ms DESC; DROP TABLE workflow_runs--"
    response = await client.get("/runs", params={"order_by": payload})
    assert response.status_code == 200
    # 表还在：注入没被执行
    assert (await client.get("/runs/count")).json()["total"] == 2


# ---------------------------------------------------------------- 分页


async def test_pagination_slices_without_overlap(client):
    await _seed(client, 3)
    first = (await client.get("/runs", params={"limit": 2, "offset": 0})).json()
    second = (await client.get("/runs", params={"limit": 2, "offset": 2})).json()
    assert len(first) == 2
    assert len(second) == 1
    ids = {row["run_id"] for row in first} | {row["run_id"] for row in second}
    assert len(ids) == 3, "两页之间出现了重复或遗漏"


async def test_include_children_toggle_changes_the_set(client):
    """子运行默认不混入列表——它们数量远多于父运行，会把父运行冲散。"""
    await _seed(client, 1)
    without = (await client.get("/runs")).json()
    with_children = (await client.get("/runs", params={"include_children": True})).json()
    assert len(without) >= 1
    assert len(with_children) >= len(without)
    assert "total" in (await client.get("/runs/count")).json()

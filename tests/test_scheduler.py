"""调度层测试：V0.2 验收标准中四条工程性质的机械检查。

1. 多前置汇合节点只执行一次；
2. 分支不设全局屏障（用时间戳证明并发，不用「看起来快了」的印象）；
3. 父节点等待子流程期间不占用子流程所需名额（无父子互等）；
4. 失败传播：阻断下游但保留已完成结果。

人造图 ``synthetic_fanout`` 就是为这四条准备的装置。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.definitions import RunStatus, NodeStatus
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.subworkflow import register_tool_executor
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings
from customer_profile.workflows import synthetic_fanout as syn

from .conftest import make_settings


def _runtime(settings: Settings, limiter: asyncio.Semaphore | None = None) -> NodeRuntime:
    limiter = limiter or asyncio.Semaphore(4)
    replay = ReplaySource(strict=False)
    return NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, replay=replay, request_limiter=limiter),
        http=HttpClient(settings, replay=replay, request_limiter=limiter),
        extra={},
    )


def _build(settings: Settings, limiter: asyncio.Semaphore | None = None):
    registry = build_default_registry()
    runtime = _runtime(settings, limiter)
    definitions = {
        syn.WORKFLOW_ID: syn.WORKFLOW,
        syn.CHILD_WORKFLOW_ID: syn.CHILD_WORKFLOW,
    }
    register_tool_executor(registry, runtime, definitions)
    scheduler = Scheduler(registry, runtime, max_active_nodes=8)
    return scheduler, runtime


@pytest.fixture(autouse=True)
def _clean_timeline():
    syn.reset_timeline()
    yield
    syn.reset_timeline()


async def _run(scheduler: Scheduler, inputs: dict | None = None):
    return await scheduler.run(
        RunRequest(
            workflow=syn.WORKFLOW,
            inputs=inputs if inputs is not None else syn.default_inputs(),
        )
    )


# ---------------------------------------------------------------- 基本跑通


async def test_synthetic_graph_runs_to_completion(tmp_path):
    scheduler, runtime = _build(make_settings(tmp_path))
    outcome = await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    assert outcome.status == RunStatus.SUCCEEDED, outcome.error
    assert outcome.outputs["result1"] == "echo:fast:p|slow:p/child-ok"


async def test_join_node_executes_exactly_once(tmp_path):
    """多前置汇合只执行一次——这是最容易在「就绪队列」实现里写错的一条。"""
    scheduler, runtime = _build(make_settings(tmp_path))
    await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    starts = [e for e in syn.timeline() if e["node_id"] == syn.JOIN and e["phase"] == "start"]
    assert len(starts) == 1, f"汇合节点执行了 {len(starts)} 次"


async def test_join_waits_for_all_predecessors(tmp_path):
    """汇合必须等全部前置完成，不能先到先执行。"""
    scheduler, runtime = _build(make_settings(tmp_path))
    await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    events = {(e["node_id"], e["phase"]): e["at"] for e in syn.timeline()}
    join_start = events[(syn.JOIN, "start")]
    for predecessor in (syn.FAST, syn.SLOW, syn.GATE):
        assert events[(predecessor, "end")] <= join_start, (
            f"汇合在 {predecessor} 结束前就开始了"
        )


async def test_branches_run_concurrently_without_global_barrier(tmp_path):
    """两个分支必须重叠执行。

    证据用「重叠时长」而不是「总耗时小于某值」：前者直接证明并发，后者只是间接指标
    且受机器负载影响。
    """
    scheduler, runtime = _build(make_settings(tmp_path))
    await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    timeline = syn.timeline()
    fast_start = next(e["at"] for e in timeline if e["node_id"] == syn.FAST and e["phase"] == "start")
    fast_end = next(e["at"] for e in timeline if e["node_id"] == syn.FAST and e["phase"] == "end")
    slow_start = next(e["at"] for e in timeline if e["node_id"] == syn.SLOW and e["phase"] == "start")
    slow_end = next(e["at"] for e in timeline if e["node_id"] == syn.SLOW and e["phase"] == "end")

    overlap = min(fast_end, slow_end) - max(fast_start, slow_start)
    assert overlap > 0, "两个分支没有任何重叠，说明存在全局屏障"


async def test_branch_exit_does_not_wait_for_slow_sibling(tmp_path):
    """快分支应先结束，而不被慢分支拖到同一条屏障上。"""
    scheduler, runtime = _build(make_settings(tmp_path))
    await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    timeline = syn.timeline()
    fast_end = next(e["at"] for e in timeline if e["node_id"] == syn.FAST and e["phase"] == "end")
    slow_end = next(e["at"] for e in timeline if e["node_id"] == syn.SLOW and e["phase"] == "end")
    assert fast_end < slow_end - (syn.SLOW_DELAY_SECONDS - syn.FAST_DELAY_SECONDS) / 2


# ---------------------------------------------------------------- 子流程


async def test_child_workflow_creates_nested_run(tmp_path):
    """子流程调用产生父节点可见的 sub_run_id。"""
    scheduler, runtime = _build(make_settings(tmp_path))
    outcome = await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    tool_node = outcome.node(syn.CHILD_CALL)
    assert tool_node is not None
    assert tool_node.sub_run_id, "子流程调用没有留下 sub_run_id"


async def test_no_parent_child_deadlock_with_single_slot(tmp_path):
    """并发名额为 1 时也不能父子互等。

    这是 V0.2 最容易踩的坑：父节点若在执行名额内等待子流程，而子流程也要名额，
    上限为 1 时必然死锁。实现上靠「子运行不申请执行名额」规避，本测试就是它的证明。
    """
    settings = make_settings(tmp_path, max_concurrent_requests=1)
    limiter = asyncio.Semaphore(1)
    registry = build_default_registry()
    runtime = _runtime(settings, limiter)
    definitions = {
        syn.WORKFLOW_ID: syn.WORKFLOW,
        syn.CHILD_WORKFLOW_ID: syn.CHILD_WORKFLOW,
    }
    register_tool_executor(registry, runtime, definitions)
    # 执行名额也只给 1 个，最大化死锁压力
    scheduler = Scheduler(registry, runtime, max_active_nodes=1)

    outcome = await asyncio.wait_for(_run(scheduler), timeout=20)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    assert outcome.status == RunStatus.SUCCEEDED, outcome.error


# ---------------------------------------------------------------- 失败传播


async def test_failure_blocks_downstream_but_keeps_results(tmp_path, monkeypatch):
    """失败传播：下游标记 blocked，已完成结果保留，运行判为失败。"""
    scheduler, runtime = _build(make_settings(tmp_path))

    async def boom(text: str) -> dict:
        raise RuntimeError("故意失败")

    monkeypatch.setitem(
        scheduler.runtime.extra.setdefault("code_functions", {}),
        "customer_profile.workflows.synthetic_fanout:slow_timer",
        boom,
    )

    outcome = await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    assert outcome.status == RunStatus.FAILED
    assert outcome.node(syn.SLOW).status == NodeStatus.FAILED
    assert outcome.node(syn.JOIN).status == NodeStatus.BLOCKED
    assert outcome.node(syn.CHILD_CALL).status == NodeStatus.BLOCKED
    # 快分支已完成，其结果必须保留（供人工核查与复用）
    assert outcome.node(syn.FAST).status == NodeStatus.SUCCEEDED


async def test_blocked_nodes_record_reason(tmp_path, monkeypatch):
    scheduler, runtime = _build(make_settings(tmp_path))

    async def boom(text: str) -> dict:
        raise RuntimeError("故意失败")

    monkeypatch.setitem(
        scheduler.runtime.extra.setdefault("code_functions", {}),
        "customer_profile.workflows.synthetic_fanout:slow_timer",
        boom,
    )
    outcome = await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    blocked = outcome.node(syn.JOIN)
    assert syn.SLOW in (blocked.error or "")


# ---------------------------------------------------------------- 并发上限


async def test_active_nodes_respect_limit(tmp_path):
    """同时处于 running 的节点数不得超过执行名额。"""
    settings = make_settings(tmp_path)
    registry = build_default_registry()
    runtime = _runtime(settings)
    definitions = {
        syn.WORKFLOW_ID: syn.WORKFLOW,
        syn.CHILD_WORKFLOW_ID: syn.CHILD_WORKFLOW,
    }
    register_tool_executor(registry, runtime, definitions)
    scheduler = Scheduler(registry, runtime, max_active_nodes=1)

    outcome = await _run(scheduler)
    await runtime.llm.aclose()
    await runtime.http.aclose()

    # 名额为 1 时，任意两个节点的执行区间不得重叠
    timeline = [(e["node_id"], e["phase"], e["at"]) for e in syn.timeline()]
    intervals: dict[str, list[float]] = {}
    for node_id, phase, at in timeline:
        intervals.setdefault(node_id, []).append(at)

    assert outcome.status == RunStatus.SUCCEEDED, outcome.error
    spanning = [
        (node_id, min(times), max(times))
        for node_id, times in intervals.items()
        if len(times) >= 2
    ]
    for i, (a_id, a_start, a_end) in enumerate(spanning):
        for b_id, b_start, b_end in spanning[i + 1 :]:
            if a_id == b_id:
                continue
            assert not (a_start < b_end and b_start < a_end), (
                f"{a_id} 与 {b_id} 的执行区间重叠，但名额只有 1 个"
            )

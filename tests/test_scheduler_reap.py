"""取消一次运行时，它名下的节点任务必须一起停。

背景（V0.6 基线真实冒烟暴露）：带单号码超时的批量脚本用 ``asyncio.wait_for`` 包住
``Scheduler.run``，超时后取消该运行，然后关闭 store。但 ``_execute_graph`` 里的节点是
``asyncio.create_task`` 出来的，而 **``asyncio.wait`` 被取消时不会连带取消它等待的任务**。
于是那批节点继续跑完：既产生无谓的外部调用（真实计费），又把留痕写进已关闭的连接而
**静默**失败（留痕层是 ``fail_soft``，不报错只打 stderr）——实测日志里刷出上百行
``Store 尚未 connect()（或已关闭）``。

这条用例钉住「取消一次运行，它名下的节点就该一起停」。
"""

from __future__ import annotations

import asyncio

from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.execution.subworkflow import register_tool_executor
from customer_profile.persistence import Store
from customer_profile.persistence.tracker import RunTracker
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings
from customer_profile.workflows import synthetic_fanout as syn

from .conftest import make_settings

SLOW_IN_FLIGHT = 5.0
"""把慢节点拉长到 5 秒：用例在它跑完之前就取消，因此不发生竞态。"""


def _build(settings: Settings, store: Store) -> tuple[Scheduler, NodeRuntime]:
    limiter = asyncio.Semaphore(4)
    replay = ReplaySource(strict=False)
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, replay=replay, request_limiter=limiter),
        http=HttpClient(settings, replay=replay, request_limiter=limiter),
        extra={},
    )
    registry = build_default_registry()
    register_tool_executor(
        registry,
        runtime,
        {syn.WORKFLOW_ID: syn.WORKFLOW, syn.CHILD_WORKFLOW_ID: syn.CHILD_WORKFLOW},
    )
    tracker = RunTracker(store, is_replay=False, code_commit="test-reap")
    scheduler = Scheduler(registry, runtime, max_active_nodes=8, recorder=tracker)
    return scheduler, runtime


def _live_node_tasks() -> list[asyncio.Task]:
    """仍在存活的节点任务。``_execute_graph`` 用 ``node:<node_id>`` 命名它们。"""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("node:")
    ]


async def _wait_for_slow_node_to_start(timeout: float = 2.0) -> None:
    """等慢节点真正开始跑，保证取消发生在**执行中途**而不是启动之前。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if any(
            entry["node_id"] == syn.SLOW and entry["phase"] == "start"
            for entry in syn.timeline()
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("慢节点在超时内没有开始执行，用例装置失效")


async def test_cancelling_a_run_reaps_its_node_tasks(tmp_path, monkeypatch):
    """取消运行后，节点任务不得继续存活。"""
    monkeypatch.setattr(syn, "SLOW_DELAY_SECONDS", SLOW_IN_FLIGHT)
    syn.reset_timeline()

    settings = make_settings(tmp_path)
    store = Store(settings.sqlite_path)
    store.connect()
    try:
        scheduler, runtime = _build(settings, store)
        task = await scheduler.submit(
            RunRequest(workflow=syn.WORKFLOW, inputs=syn.default_inputs())
        )
        await _wait_for_slow_node_to_start()

        assert _live_node_tasks(), "取消之前应当有节点在跑，否则用例没有测到东西"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 取消已生效，且不再有节点任务存活。给一个极短的宽限：取消到任务真正结束之间
        # 有一小段调度延迟，但远小于被取消的那个 5 秒 sleep。
        await asyncio.sleep(0)

        leftover = _live_node_tasks()
        assert not leftover, (
            "运行被取消后仍有节点任务存活（会继续出网并写已关闭的 store）："
            f"{[t.get_name() for t in leftover]}"
        )

        await runtime.llm.aclose()
        await runtime.http.aclose()
    finally:
        syn.reset_timeline()
        store.close()


async def test_cancelling_a_run_lets_the_store_close_cleanly(tmp_path, monkeypatch, capsys):
    """取消后关库，不该再有「Store 尚未 connect()（或已关闭）」的写入告警。

    这条同时是回归的**症状断言**：修复前，游离的节点在关库之后才写库，留痕层 ``fail_soft``
    只把错误打到 stderr——因此「没有告警」正是「没有游离写入」的可观测证据。
    """
    monkeypatch.setattr(syn, "SLOW_DELAY_SECONDS", SLOW_IN_FLIGHT)
    syn.reset_timeline()

    settings = make_settings(tmp_path)
    store = Store(settings.sqlite_path)
    store.connect()

    scheduler, runtime = _build(settings, store)
    task = await scheduler.submit(
        RunRequest(workflow=syn.WORKFLOW, inputs=syn.default_inputs())
    )
    await _wait_for_slow_node_to_start()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    await runtime.llm.aclose()
    await runtime.http.aclose()
    store.close()

    # 关库之后让事件循环再转几圈：若仍有游离任务，它此刻会尝试写库并触发告警。
    for _ in range(5):
        await asyncio.sleep(0)

    captured = capsys.readouterr()
    assert "已关闭" not in captured.err, (
        f"关库后仍有留痕写入尝试（说明有游离任务）：\n{captured.err[:500]}"
    )
    syn.reset_timeline()

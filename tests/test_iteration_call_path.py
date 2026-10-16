"""回归：并发兄弟节点不得继承循环体的 ``call_path``。

**这是一个真实缺陷的回归测试**（2026-10-16 业务测试臂实测暴露，运行
``4793fef1644d47928f4f55bc49c4d1ef``）。

缺陷机制：``_execute_node`` 为循环体节点临时改写 ``_RunRecord.call_path``（整运行共用
一份句柄），在**整个 await 期间**保持覆盖值。运行内各节点是并发的，于是循环体改写
期间结束的兄弟节点，在留痕层读到被污染的 ``run.call_path``
（``tracker.node_finished`` 用 ``execution.call_path or run.call_path`` 取值，
而顶层节点的 ``execution.call_path`` 是空串，为 falsy，必然回落到 run 上的值）。

后果有两个，都是静默的：

1. 该节点的终态被写进**另一条调用路径**，它自己那条由 ``node_started`` 写下的
   ``running`` 行再无人更新——界面永久显示「运行中」（实测「极光数据」）；
2. 嵌套迭代把覆盖值当基路径叠加，拼出 ``/0/0/iter:X/0/iter:X/0`` 这类重复段。

断言口径：顶层节点的 `node_started` 与 `node_finished` 必须落在**同一条** `call_path`
（此处为空串），且不得出现任何带 ``iter:`` 的行。

装置与 ``test_iteration_nesting.py`` 同思路：真实 Scheduler、真实 RunTracker、真实
SQLite；只有节点执行器是自定义的慢节点，不出网。
"""

from __future__ import annotations

import asyncio

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import (
    ExecutionOutcome,
    NodeRuntime,
    build_default_registry,
)
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.persistence import RunTracker, Store
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

START = "n_start"
END = "n_end"
SLOW = "n_slow"
ITER = "n_iter"
BODY = f"{ITER}_body"
BODY_START = f"{ITER}_start"

SLOW_MS = 0.05
"""顶层兄弟节点的耗时。要**短于**循环体节点的耗时，才能落在覆盖窗口内结束。"""
BODY_MS = 0.20
"""循环体节点的耗时。覆盖窗口由它撑开。"""


def _execute_slow(seconds: float):
    """构造一个「睡够时间再成功」的执行器。耗时是可控的，因此本用例不依赖调度运气。"""

    async def execute(node, ctx, rt) -> ExecutionOutcome:  # noqa: ARG001
        await asyncio.sleep(seconds)
        return ExecutionOutcome(outputs={"value": node.node_id})

    return execute


def _workflow() -> WorkflowDef:
    """start → (慢节点, 迭代) → end。

    慢节点与迭代**都只依赖 start**，因此在 ``max_active_nodes`` 允许时并发启动——
    这正是缺陷成立的形状。
    """
    body_nodes = [
        make_node(
            BODY_START,
            "循环体入口",
            "iteration-start",
            outputs=("item", "index"),
            iteration_id=ITER,
        ),
        make_node(
            BODY,
            "循环体慢节点",
            "slow_body",
            after=(BODY_START,),
            inputs={"item": (BODY_START, "item")},
            outputs=("value",),
            iteration_id=ITER,
        ),
    ]
    return WorkflowDef(
        workflow_id="call_path_probe",
        display_name="call_path 隔离探针",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"output": ITER},
        nodes=(
            make_node(START, "输入", "start", variables=("phone_number", "items")),
            make_node(
                SLOW, "同图慢节点", "slow_top", after=(START,), outputs=("value",)
            ),
            make_node(
                ITER,
                "迭代",
                "iteration",
                after=(START,),
                outputs=("output", "item"),
                iterator=(START, "items"),
                inputs={"@output": (BODY, "value")},
                body=tuple(n.node_id for n in body_nodes),
                flatten_output=True,
                error_handle_mode="terminated",
            ),
            *body_nodes,
            make_node(
                END,
                "输出",
                "end",
                after=(ITER,),
                inputs={"output": (ITER, "output")},
                outputs=("output",),
            ),
        ),
    )


def _runtime(settings: Settings) -> NodeRuntime:
    limiter = asyncio.Semaphore(4)
    replay = ReplaySource(strict=False)
    return NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, replay=replay, request_limiter=limiter),
        http=HttpClient(settings, replay=replay, request_limiter=limiter),
        extra={},
    )


async def test_sibling_finishing_inside_iteration_window_keeps_its_own_call_path(tmp_path):
    """顶层慢节点在循环体覆盖窗口内结束，仍必须记在自己那条路径上。

    修复前：慢节点的终态被写进 ``/iter:n_iter/0``，而它自己在 ``''`` 上的
    ``running`` 行永久残留——正是界面显示「运行中」的那种行。
    """
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "trace.db")
    store.connect()
    try:
        runtime = _runtime(settings)
        registry = build_default_registry()
        registry.register("slow_top", _execute_slow(SLOW_MS))
        registry.register("slow_body", _execute_slow(BODY_MS))
        tracker = RunTracker(store, is_replay=False, code_commit="test-call-path")
        scheduler = Scheduler(
            registry, runtime, max_active_nodes=8, recorder=tracker
        )

        outcome = await scheduler.run(
            RunRequest(
                workflow=_workflow(),
                inputs={"phone_number": "13415100087", "items": ["a", "b"]},
            )
        )
        assert outcome.succeeded, outcome.error

        rows = await store.list_node_executions(outcome.run_id)
        slow_rows = [r for r in rows if r["node_id"] == SLOW]
        assert len(slow_rows) == 1, (
            "同一个顶层节点出现了多条记录——终态被写到了另一条调用路径上："
            f"{[(r['call_path'], r['status']) for r in slow_rows]}"
        )
        assert slow_rows[0]["call_path"] == "", (
            f"顶层节点的调用路径必须为空串，实际 {slow_rows[0]['call_path']!r}"
        )
        assert slow_rows[0]["status"] == "succeeded"

        # 全库口径：不得有任何被记到迭代路径下的**非循环体**节点。
        body_ids = {BODY, BODY_START}
        stray = [
            (r["node_id"], r["call_path"])
            for r in rows
            if "iter:" in (r["call_path"] or "") and r["node_id"] not in body_ids
        ]
        assert not stray, f"非循环体节点被记进了迭代路径：{stray}"

        # 循环体自身的逐轮留痕不受影响：每轮一条，路径互不相同。
        body_rows = [r for r in rows if r["node_id"] == BODY]
        assert len(body_rows) == 2, f"循环体应逐项留痕 2 条，实际 {len(body_rows)}"
        assert len({r["call_path"] for r in body_rows}) == 2
    finally:
        store.close()

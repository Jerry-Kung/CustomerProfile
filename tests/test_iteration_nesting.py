"""复现：同图内 tool 节点先于 iteration 节点执行时，迭代循环体入口被偷换。

缺陷机制（V0.6 基线真实冒烟暴露，`svc` 侧可复现）：

1. ``Scheduler.__init__``（`scheduler.py:145`）**无条件**把 ``self.run_iteration_body_node``
   写到**共享的** ``runtime.iteration_node_runner`` 槽位上——一个进程内所有调度器共用
   同一个 ``NodeRuntime``，后构造的直接覆盖先前的。
2. ``SubWorkflowRunner.__call__`` 每次调用都 new 一个 ``Scheduler``（未传
   ``scheduler_factory`` 时的回落分支），因为「子运行必须沿用父运行的留痕器」。
   于是**子运行一构造就把槽位改指到子调度器上**。
3. ``run_iteration_body_node`` 读的是**每实例单槽**的 ``self._current_record``。
   被改指到的子调度器早已跑完，``_current_record`` 已置回 ``None``。
4. 父图随后执行 iteration 节点——``executors_iteration.py:89`` 在循环体执行**之前**
   读取槽位一次——取到的 runner 属于**别的**调度器，于是抛
   ``RuntimeError: run_iteration_body_node 必须在一次运行内调用``。

真实触发点：``test_drive_audio`` / ``outbound_call_audio`` 的图里 tool 节点与 iteration
节点同图，且 tool 先执行。用户在 V0.6 冒烟中实测到 ``13430349943`` 因此整条失败
（``evidence_subagent`` → ``test_drive_audio`` → iteration 节点）。

两个用例：
- ``test_iteration_after_sibling_tool_node``：**缺陷主因**。同图 tool 先执行，iteration
  后执行。这是真实失败的形状。
- ``test_iteration_body_containing_tool_node``：边界用例。循环体**内部**含 tool 节点。
  循环体的 runner 在进入循环前就被读走并缓存，因此这条路径预期**不受影响**——把它写下来
  是为了钉住「缺陷在读取时机之前，而不是在循环体内部」这个结论。

装置与 ``test_iteration.py`` / ``test_entry_end_to_end.py`` 同思路：真实 Scheduler、
真实 tool 执行器，只有子流程是微缩的。全部不出网。
"""

from __future__ import annotations

import asyncio

import pytest

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.execution.subworkflow import register_tool_executor
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

START = "n_start"
END = "n_end"
TOOL = "n_tool"
ITER = "n_iter"
LEAF_ID = "nest_leaf"


def _leaf() -> WorkflowDef:
    """子流程：start → 模板 → end。无 iteration，只用来让 tool 节点真的开一次子运行。"""
    return WorkflowDef(
        workflow_id=LEAF_ID,
        display_name="叶子（微缩）",
        source_dsl=None,
        entries=("c_start",),
        exits=("c_end",),
        outputs={"deep_value": "c_src"},
        nodes=(
            make_node("c_start", "输入", "start", variables=("phone_number",)),
            make_node(
                "c_src", "产出", "template-transform", after=("c_start",),
                inputs={"phone_number": ("c_start", "phone_number")},
                outputs=("output",), template_text="{{ phone_number }}",
            ),
            make_node(
                "c_end", "输出", "end", after=("c_src",),
                inputs={"deep_value": ("c_src", "output")}, outputs=("deep_value",),
            ),
        ),
    )


def _body(iteration_id: str, *, with_tool: bool) -> list:
    """循环体：[iteration-start] (+ 可选 tool) → 模板回显。

    ``with_tool=True`` 时循环体内含 tool 节点，用于边界用例。
    """
    start = make_node(
        f"{iteration_id}_start", "", "iteration-start",
        outputs=("item", "index"), iteration_id=iteration_id,
    )
    nodes = [start]
    if with_tool:
        nodes.append(
            make_node(
                f"{iteration_id}_tool", "循环体内子流程", "tool",
                after=(f"{iteration_id}_start",),
                inputs={"phone_number": (START, "phone_number")},
                outputs=("deep_value",), **{"@workflow": LEAF_ID},
            )
        )
    nodes.append(
        make_node(
            f"{iteration_id}_body", "回显", "template-transform",
            after=(nodes[-1].node_id,),
            inputs={"item": (iteration_id, "item")},
            outputs=("output",), template_text="{{ item }}",
            iteration_id=iteration_id,
        )
    )
    return nodes


def _workflow(*, iteration_after_tool: bool, body_with_tool: bool) -> WorkflowDef:
    """两种形状：

    - ``iteration_after_tool=True``：start → tool → iteration → end（缺陷主因所在）
    - ``iteration_after_tool=False``：start → iteration → end，且循环体内含 tool
    """
    body = _body(ITER, with_tool=body_with_tool)
    nodes: list = [make_node(START, "输入", "start", variables=("phone_number", "items"))]

    if iteration_after_tool:
        nodes.append(
            make_node(
                TOOL, "同图子流程", "tool", after=(START,),
                inputs={"phone_number": (START, "phone_number")},
                outputs=("deep_value",), **{"@workflow": LEAF_ID},
            )
        )
        iter_after = (TOOL,)
    else:
        iter_after = (START,)

    nodes.append(
        make_node(
            ITER, "迭代", "iteration", after=iter_after,
            outputs=("output", "item"),
            iterator=(START, "items"),
            inputs={"@output": (f"{ITER}_body", "output")},
            body=tuple(n.node_id for n in body),
            flatten_output=True, error_handle_mode="terminated",
        )
    )
    nodes.extend(body)
    nodes.append(
        make_node(
            END, "输出", "end", after=(ITER,),
            inputs={"output": (ITER, "output")}, outputs=("output",),
        )
    )
    return WorkflowDef(
        workflow_id="nest_probe",
        display_name="嵌套探针",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"output": ITER},
        nodes=tuple(nodes),
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


async def _run(graph: WorkflowDef, tmp_path):
    settings = make_settings(tmp_path)
    runtime = _runtime(settings)
    registry = build_default_registry()
    scheduler = Scheduler(registry, runtime, max_active_nodes=8)
    register_tool_executor(registry, runtime, {LEAF_ID: _leaf()})
    return await scheduler.run(
        RunRequest(
            workflow=graph,
            inputs={"phone_number": "13415100087", "items": ["a", "b"]},
        )
    )


async def test_iteration_after_sibling_tool_node(tmp_path):
    """同图 tool 先执行、iteration 后执行 —— 这正是真实失败形状。

    修复前：iteration 节点取到的 runner 属于 tool 节点刚构造出来的**子调度器**，
    其 ``_current_record`` 已为 ``None``，抛
    ``RuntimeError: run_iteration_body_node 必须在一次运行内调用``。
    """
    outcome = await _run(
        _workflow(iteration_after_tool=True, body_with_tool=False), tmp_path
    )
    iteration = next(e for e in outcome.node_executions if e.node_id == ITER)
    assert iteration.status == "succeeded", (
        f"iteration 节点应当在同图 tool 节点之后仍能执行；实际 "
        f"status={iteration.status} error={iteration.error!r}"
    )
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["output"] == ["a", "b"]


async def test_iteration_body_containing_tool_node(tmp_path):
    """循环体**内部**含 tool 节点 —— 预期不受影响（runner 在进循环前已缓存）。

    这条是边界用例：它说明缺陷的触发点在「读取槽位的时机」（iteration 节点开始执行时），
    而不是「循环体内部有没有开子运行」。
    """
    outcome = await _run(
        _workflow(iteration_after_tool=False, body_with_tool=True), tmp_path
    )
    iteration = next(e for e in outcome.node_executions if e.node_id == ITER)
    assert iteration.status == "succeeded", (
        f"循环体内含 tool 时迭代仍应成功；实际 "
        f"status={iteration.status} error={iteration.error!r}"
    )
    assert outcome.succeeded, outcome.error


async def test_concurrent_runs_do_not_share_iteration_runner(tmp_path):
    """并发运行不得互相偷换循环体入口。

    ``_current_record`` 是**每实例单槽**。同一 Scheduler 上并发跑两个含 iteration 的
    运行，A 的循环体可能被派发给 B 的记录（或调用时 B 已跑完、槽位为 ``None``）。
    V0.6 基线脚本用 4 并发，这条路径必须成立。
    """
    settings = make_settings(tmp_path)
    runtime = _runtime(settings)
    registry = build_default_registry()
    scheduler = Scheduler(registry, runtime, max_active_nodes=8)

    graphs = [
        _workflow(iteration_after_tool=False, body_with_tool=False)
        for _ in range(4)
    ]
    outcomes = await asyncio.gather(
        *(
            scheduler.run(
                RunRequest(
                    workflow=g,
                    inputs={"phone_number": "13415100087", "items": ["a", "b"]},
                )
            )
            for g in graphs
        )
    )
    for index, outcome in enumerate(outcomes):
        iteration = next(e for e in outcome.node_executions if e.node_id == ITER)
        assert iteration.status == "succeeded", (
            f"第 {index} 个并发运行的 iteration 失败："
            f"status={iteration.status} error={iteration.error!r}"
        )
        assert outcome.outputs["output"] == ["a", "b"]

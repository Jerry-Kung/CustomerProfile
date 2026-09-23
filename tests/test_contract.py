"""对外契约：入参口径与最终产出形态。

主入口 `（新）客户初始画像（生产环境）` 是外部系统唯一的调用面
（`Dify迁移任务说明.md` §3.1）。它的入参名、必填性、以及 ``result1`` 的类型
一旦漂移，上游调用方就会静默出错——因此这里逐项钉住，而不是依赖某个端到端用例
顺带覆盖。

本文件**不下载、不调用模型**，只断言定义层与一次最小运行的结果形态。
"""

from __future__ import annotations

import asyncio

import pytest

from customer_profile.definitions import NodeStatus
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings
from customer_profile.workflows import customer_profile_entry as entry

from .conftest import make_settings


# ---------------------------------------------------------------- 入参契约


def test_entry_inputs_are_phone_number_and_batch_id():
    """入参名固定为 ``phone_number`` 与 ``batch_id``，不多不少。"""
    start = entry.WORKFLOW.node_map[entry.START]
    assert tuple(start.config["variables"]) == ("phone_number", "batch_id")


def test_phone_number_is_required():
    """``phone_number`` 必填——缺它整条链路没有任何可查的客户。"""
    start = entry.WORKFLOW.node_map[entry.START]
    assert start.config["required_phone_number"] is True
    assert start.config["required_batch_id"] is False


def test_batch_id_is_optional_and_defaults_to_empty():
    """``batch_id`` 可选，缺省为空串（上游批量任务未给时也要能跑）。"""
    assert entry.default_inputs("13800000000") == {
        "phone_number": "13800000000",
        "batch_id": "",
    }


def test_entry_exposes_exactly_result1():
    """对外输出只有 ``result1`` 一个字段。"""
    assert entry.WORKFLOW.outputs == {"result1": "result1"}


# ---------------------------------------------------------------- 产出形态


class _StubSubWorkflows:
    """把三个 tool 节点的子流程换成固定产出。

    契约测试关心的是**主入口自身的入参口径与产出形态**，不是子流程内部实现
    （那由各自的测试与端到端用例覆盖）。这里让子流程返回符合既有声明的结果，
    使主入口能在不接模型、不出网的前提下跑完。
    """

    def __init__(self) -> None:
        from customer_profile.execution.executors import ExecutionOutcome

        self._outcome_cls = ExecutionOutcome

    async def __call__(self, *, workflow_id: str, inputs, ctx, node, call_path):
        outputs = {
            entry.EVIDENCE_WORKFLOW: {
                "result": [{"证据": "stub"}],
                "original_data": {"stub": True},
            },
            entry.PROFILE_WORKFLOW: {
                "profile_result": "画像",
                "hobby_result": "兴趣",
                "style_result": "消费风格",
            },
            entry.PRODUCTION_WORKFLOW: {"result1": "最终画像文本"},
        }[workflow_id]
        return self._outcome_cls(outputs=outputs, meta={"sub_run_id": f"stub:{workflow_id}"})


def _runtime(settings: Settings, stub) -> NodeRuntime:
    limiter = asyncio.Semaphore(4)
    replay = ReplaySource(strict=False)
    return NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, replay=replay, request_limiter=limiter),
        http=HttpClient(settings, replay=replay, request_limiter=limiter),
        extra={"subworkflow_runner": stub},
    )


async def test_entry_result1_is_a_string(tmp_path):
    """``result1`` 必须是**字符串**。

    这是最容易在迁移中被无意改掉的契约：Dify 的 ``end`` 节点原样透传，而子流程内部
    的结构化结果（证据数组、三段画像）很容易被顺手一并带出来。上游按文本消费，
    拿到对象就会出错。
    """
    settings = make_settings(tmp_path)
    stub = _StubSubWorkflows()
    registry = build_default_registry()
    registry.register("tool", _stub_tool_executor(stub))
    scheduler = Scheduler(registry, _runtime(settings, stub), max_active_nodes=8)

    outcome = await scheduler.run(
        RunRequest(
            workflow=entry.WORKFLOW,
            inputs=entry.default_inputs("13800000000", "batch-1"),
        )
    )
    assert outcome.succeeded, outcome.error
    assert set(outcome.outputs) == {"result1"}
    assert isinstance(outcome.outputs["result1"], str), (
        f"result1 应为字符串，实际 {type(outcome.outputs['result1']).__name__}"
    )


def _stub_tool_executor(stub):
    """把 ``tool`` 节点的子流程调用换成 :class:`_StubSubWorkflows`。"""
    from customer_profile.execution.executors import ExecutionOutcome

    async def execute_tool(node, ctx, rt) -> ExecutionOutcome:
        from customer_profile.execution.subworkflow import CallPath, _next_call_path

        return await stub(
            workflow_id=node.config["@workflow"],
            inputs={},
            ctx=ctx,
            node=node,
            call_path=CallPath(),
        )

    return execute_tool


async def test_entry_tool_nodes_declare_subworkflows():
    """三个 tool 节点各自显式引用子流程 ID，不按名称猜。"""
    for node_id in (entry.EVIDENCE, entry.PROFILE, entry.PRODUCTION):
        node = entry.WORKFLOW.node_map[node_id]
        assert node.node_type == "tool"
        assert node.config.get("@workflow"), f"{node_id} 未声明 @workflow"


def test_entry_graph_shape_is_five_nodes_four_edges():
    """有意差异 W1：入口图是 5 节点 / 4 边（删去了冗余的第二个回写节点）。"""
    assert len(entry.WORKFLOW.nodes) == 5
    assert len(entry.WORKFLOW.edge_list()) == 4


def test_entry_removed_writeback_node_is_absent():
    """W1 删除的节点确实不在图里，且它引用的边也没有残留。"""
    assert entry.REMOVED_NODES[0] not in entry.WORKFLOW.node_map
    for source, target in entry.REMOVED_EDGES:
        assert (source, target) not in {
            (e["source"], e["target"]) for e in entry.WORKFLOW.edge_list()
        }

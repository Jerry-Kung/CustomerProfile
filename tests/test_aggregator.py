"""聚合器：普通形态与分组形态的取值口径。

两条被验证的性质来自差异清单：

- **P4：判据是「来源是否真的执行过」，不是「值是否非空」。** 空串是合法产出
  （``无数据，输出默认信息`` 模板的正常结果就是一段固定文本；各截图流程的 code 节点
  在「没找到该 channel」时也明确返回空串）。若按非空判定，这些正常结果会被误判成
  缺失，聚合器转而取另一支的值，业务结果被静默改变。
- **P3：分组形态每组包成 ``{"output": 值}``。** DSL 下游 code 取的是
  ``hobby_result_object["output"]``，并把这些入参声明为 ``value_type: object``。
"""

from __future__ import annotations

import asyncio

import pytest

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

SHARED = "customer_profile.workflows.shared_code"


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


async def _run(workflow: WorkflowDef, inputs: dict, tmp_path):
    settings = make_settings(tmp_path)
    registry = build_default_registry()
    scheduler = Scheduler(registry, _runtime(settings), max_active_nodes=8)
    return await scheduler.run(RunRequest(workflow=workflow, inputs=inputs))


def _plain_workflow() -> WorkflowDef:
    """``start → A ─┐``、``start → B ─┴→ aggregator → end``。

    A 与 B 都是模板节点：A 产出空串（合法结果），B 产出非空。聚合器按声明顺序
    先看 A——若按「非空」取，就会错误地跳过 A。
    """
    return WorkflowDef(
        workflow_id="aggregator_plain_probe",
        display_name="聚合器-普通形态验证图",
        source_dsl=None,
        entries=("a_start",),
        exits=("a_end",),
        outputs={"result": "output"},
        nodes=(
            make_node("a_start", "输入", "start", variables=("text",)),
            make_node(
                "a_empty", "产出空串", "template-transform", after=("a_start",),
                outputs=("output",), template_text='{{ "" }}',
            ),
            make_node(
                "a_filled", "产出非空", "template-transform", after=("a_start",),
                inputs={"text": ("a_start", "text")},
                outputs=("output",), template_text="{{ text }}",
            ),
            make_node(
                "a_agg", "聚合", "variable-aggregator",
                after=("a_empty", "a_filled"),
                variables=(("a_empty", "output"), ("a_filled", "output")),
                outputs=("output",),
            ),
            make_node(
                "a_end", "输出", "end", after=("a_agg",),
                inputs={"result": ("a_agg", "output")}, outputs=("result",),
            ),
        ),
    )


def _grouped_workflow() -> WorkflowDef:
    """分组聚合：两个组各取一个来源，组名 ``hobby`` / ``consumption``。"""
    return WorkflowDef(
        workflow_id="aggregator_grouped_probe",
        display_name="聚合器-分组形态验证图",
        source_dsl=None,
        entries=("g_start",),
        exits=("g_end",),
        outputs={"hobby_result": "hobby_result", "consumption_result": "consumption_result"},
        nodes=(
            make_node("g_start", "输入", "start", variables=("hobby", "style")),
            make_node(
                "g_hobby_src", "兴趣来源", "template-transform", after=("g_start",),
                inputs={"hobby": ("g_start", "hobby")},
                outputs=("output",), template_text="{{ hobby }}",
            ),
            make_node(
                "g_style_src", "风格来源", "template-transform", after=("g_start",),
                inputs={"style": ("g_start", "style")},
                outputs=("output",), template_text="{{ style }}",
            ),
            make_node(
                "g_agg", "特征结果聚合", "variable-aggregator",
                after=("g_hobby_src", "g_style_src"),
                outputs=("hobby", "consumption"),
                groups=(
                    {"group_name": "hobby", "output_type": "string",
                     "variables": (("g_hobby_src", "output"),)},
                    {"group_name": "consumption", "output_type": "string",
                     "variables": (("g_style_src", "output"),)},
                ),
            ),
            make_node(
                "g_split", "代码执行", "code", after=("g_agg",),
                inputs={
                    "hobby_result_object": ("g_agg", "hobby"),
                    "consumption_result_object": ("g_agg", "consumption"),
                },
                outputs=("consumption_result", "hobby_result"),
                function=f"{SHARED}:split_grouped_features",
            ),
            make_node(
                "g_end", "输出", "end", after=("g_split",),
                inputs={
                    "hobby_result": ("g_split", "hobby_result"),
                    "consumption_result": ("g_split", "consumption_result"),
                },
                outputs=("hobby_result", "consumption_result"),
            ),
        ),
    )


async def test_aggregator_takes_first_executed_even_when_empty(tmp_path):
    """P4：先执行过的来源即便产出空串，也优先于后一个非空来源。"""
    outcome = await _run(_plain_workflow(), {"text": "非空内容"}, tmp_path)
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["result"] == "", (
        f"聚合器应取「先执行过」的 a_empty（空串），实际取了 {outcome.outputs['result']!r}——"
        "说明判据退化成了「值非空」，会静默改变业务结果"
    )

    agg = next(e for e in outcome.node_executions if e.node_id == "a_agg")
    assert agg.outputs["output"] == ""


async def test_grouped_aggregator_wraps_each_group_in_output(tmp_path):
    """P3：每组是 ``{"output": 值}``，下游 code 才能按 ``["output"]`` 取。"""
    outcome = await _run(
        _grouped_workflow(), {"hobby": "钓鱼", "style": "务实"}, tmp_path
    )
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["hobby_result"] == "钓鱼"
    assert outcome.outputs["consumption_result"] == "务实"

    agg = next(e for e in outcome.node_executions if e.node_id == "g_agg")
    assert agg.outputs["hobby"] == {"output": "钓鱼"}, (
        f"分组值应是对象 {{'output': ...}}，实际 {agg.outputs['hobby']!r}"
    )
    assert agg.outputs["consumption"] == {"output": "务实"}

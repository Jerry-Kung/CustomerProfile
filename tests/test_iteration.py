"""迭代节点：逐项执行、按序留痕、失败即终止。

V0.3 新增 ``iteration`` 语义后的第一批断言。用一个人造小图验证，不依赖真实 DSL：
真实工作流里的迭代体含 HTTP 与 vision 调用，验证成本高且与「迭代语义本身」无关。

被验证的三条，都是 9 个含迭代工作流（7 个截图类 + 2 个录音类）共同依赖的性质：

1. 逐项执行 —— 3 个元素就该有 3 轮留痕，且每轮 ``call_path`` 互不相同；
   若相同，``node_executions`` 的唯一键会把多轮压成一条，逐项追溯就没了。
2. 循环体能读到当前项 —— 定义里写的是 ``{{#<迭代节点>.item#}}``，而循环体执行时
   迭代节点尚未产出结果，因此这必须由上下文单独支撑。
3. ``error_handle_mode=terminated`` —— 某一项失败即终止整轮，且已完成项的结果保留。
"""

from __future__ import annotations

import asyncio

from customer_profile.definitions import NodeStatus, WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

START = "probe_start"
ITER = "probe_iter"
ITER_START = "probe_iter_start"
BODY = "probe_body"
BODY_FAIL = "probe_body_fail"
END = "probe_end"


def _build_workflow(*, fail_on_third: bool = False) -> WorkflowDef:
    """构造一个最小迭代图：start → iteration → end。

    循环体用 ``template-transform`` 而不是 ``code``：它按绑定名取值，正好把
    「循环体读到当前项」这件事单独暴露出来，不受任何业务代码影响。
    """
    body_nodes = [
        make_node(
            ITER_START,
            "",
            "iteration-start",
            outputs=("item", "index"),
            iteration_id=ITER,
        ),
    ]
    if fail_on_third:
        # 循环体恒失败：模板严格模式引用了**没有绑定**的变量，第一项就会报错。
        # 用「缺变量」而不是「某个特定输入值」来触发失败，失败点因此是确定的。
        # 变量名必须是 ASCII：模板的 ``{{ 名字 }}`` 只认 ASCII 标识符，中文占位符
        # 不会被当作占位符，那样这条用例会静默地「永远成功」。
        body_nodes.append(
            make_node(
                BODY_FAIL,
                "循环体-必失败",
                "template-transform",
                after=(ITER_START,),
                inputs={"item": (ITER, "item")},
                outputs=("output",),
                template_text="{{ missing_var }}",
                iteration_id=ITER,
            )
        )
        exit_body = BODY_FAIL
    else:
        body_nodes.append(
            make_node(
                BODY,
                "循环体-回显",
                "template-transform",
                after=(ITER_START,),
                inputs={"item": (ITER, "item")},
                outputs=("output",),
                template_text="{{ item }}",
                iteration_id=ITER,
            )
        )
        exit_body = BODY

    return WorkflowDef(
        workflow_id="iteration_probe",
        display_name="迭代验证图",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"output": "output"},
        nodes=(
            make_node(START, "输入", "start", variables=("items",)),
            make_node(
                ITER,
                "迭代",
                "iteration",
                after=(START,),
                outputs=("output", "item"),
                iterator=(START, "items"),
                inputs={"@output": (exit_body, "output")},
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


async def _run(workflow: WorkflowDef, inputs: dict, tmp_path):
    settings = make_settings(tmp_path)
    registry = build_default_registry()
    scheduler = Scheduler(registry, _runtime(settings), max_active_nodes=8)
    return await scheduler.run(RunRequest(workflow=workflow, inputs=inputs))


async def test_iteration_body_runs_once_per_item(tmp_path):
    """3 个元素 → 循环体留痕 3 条，且 call_path 互不相同。"""
    workflow = _build_workflow()
    outcome = await _run(workflow, {"items": ["a", "b", "c"]}, tmp_path)
    assert outcome.succeeded, outcome.error

    body_records = [e for e in outcome.node_executions if e.node_id == BODY]
    assert len(body_records) == 3, (
        f"循环体应执行 3 次，实际 {len(body_records)} 次；"
        "只记 1 条说明多轮被唯一键压成了一条"
    )
    paths = {e.call_path for e in body_records}
    assert len(paths) == 3, f"每轮的 call_path 必须互不相同，实际 {paths}"
    assert all("iter:" in p for p in paths), paths


async def test_iteration_body_sees_current_item(tmp_path):
    """循环体读到的必须是**当前项**，逐项不同。

    定义里写的是 ``{{#<迭代节点>.item#}}``。循环体执行时迭代节点尚未产出结果，
    因此这条断言实质上在验证上下文对当前项的单独支撑——它一坏，7 个截图流程与
    2 个录音流程的循环体全部拿不到文件地址。
    """
    workflow = _build_workflow()
    outcome = await _run(workflow, {"items": ["first", "second", "third"]}, tmp_path)
    assert outcome.succeeded, outcome.error

    collected = outcome.outputs["output"]
    assert collected == ["first", "second", "third"], (
        f"迭代应逐项产出原文，实际 {collected!r}——"
        "若全是同一项，说明 item 没有随轮次更新"
    )


async def test_iteration_flattens_output(tmp_path):
    """``flatten_output=true`` 时结果是一个平铺数组（实测全部如此）。"""
    workflow = _build_workflow()
    outcome = await _run(workflow, {"items": ["x", "y"]}, tmp_path)
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["output"] == ["x", "y"]


async def test_iteration_stops_on_first_failure(tmp_path):
    """``error_handle_mode=terminated``：某项失败即终止整轮，运行失败而非静默跳过。"""
    workflow = _build_workflow(fail_on_third=True)
    outcome = await _run(workflow, {"items": ["a", "b", "c"]}, tmp_path)
    assert not outcome.succeeded, "循环体失败必须让整轮失败，不能静默产出"

    iter_record = next(e for e in outcome.node_executions if e.node_id == ITER)
    assert iter_record.status == NodeStatus.FAILED
    assert "第 0 项失败" in (iter_record.error or ""), (
        f"错误信息应指出失败发生在第几项，实际：{iter_record.error!r}"
    )

    # terminated 的语义是「立刻停」，不是「跳过坏项继续跑」
    assert len([e for e in outcome.node_executions if e.node_id == BODY_FAIL]) == 1, (
        "第一项失败后应立即终止，不该继续执行后续轮次"
    )

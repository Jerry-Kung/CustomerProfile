"""主入口端到端：真实嵌套运行树。

与 ``test_contract.py`` 的分工：那边用**替身执行器**钉住入参口径与 ``result1`` 形态；
这里用**真实的 tool 执行器与真实的嵌套调度器**，只把三个子工作流换成同形状的微缩图。
因此本文件验证的是「主入口怎么把子运行串起来」——``call_path``、``parent_run_id``、
``sub_run_id`` 是否真的成立——而不是子流程内部的业务逻辑（各由自己的测试覆盖）。

这样做是刻意的取舍：若让子流程照真图跑，fixture 必须覆盖 14 路扇出与全部提示词，
体积与维护成本都不合理，而本文件要证明的那几件事（嵌套关系、父节点透出子流程输出、
子运行不占执行名额）与子流程内容无关。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from customer_profile.definitions import NodeStatus, WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.execution.subworkflow import register_tool_executor
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings
from customer_profile.workflows import customer_profile_entry as entry

from .conftest import FIXTURE_DIR, make_settings


def _child(workflow_id: str, name: str, outputs: dict[str, str]) -> WorkflowDef:
    """一个微缩子流程：start → 模板节点 → end，产出 ``outputs`` 声明的字段。

    形状与真实子流程一致（有 start、有 end、有具名输出），因此父节点透出输出的
    路径与真实情况相同；内容则是固定文本，不涉及任何外部调用。
    """
    fields = tuple(outputs)
    nodes = [make_node("c_start", "输入", "start", variables=("phone_number",))]
    end_inputs: dict[str, tuple[str, str]] = {}
    src_ids: list[str] = []
    for index, field_name in enumerate(fields):
        node_id = f"c_src_{index}"
        src_ids.append(node_id)
        # 模板节点本身也要绑定 phone_number：模板的 ``{{ phone_number }}`` 按入参名取值，
        # 不绑定就是空入参，运行期会以「未提供」直接失败——与真实子流程的写法一致。
        nodes.append(
            make_node(
                node_id,
                f"产出 {field_name}",
                "template-transform",
                after=("c_start",),
                inputs={"phone_number": ("c_start", "phone_number")},
                outputs=("output",),
                template_text="{{ phone_number }}",
            )
        )
        end_inputs[field_name] = (node_id, "output")
    nodes.append(
        make_node(
            "c_end", "输出", "end", after=tuple(src_ids),
            inputs=end_inputs, outputs=fields,
        )
    )
    return WorkflowDef(
        workflow_id=workflow_id,
        display_name=name,
        source_dsl=None,
        entries=("c_start",),
        exits=("c_end",),
        outputs=outputs,
        nodes=tuple(nodes),
    )


def _children() -> dict[str, WorkflowDef]:
    return {
        entry.EVIDENCE_WORKFLOW: _child(
            entry.EVIDENCE_WORKFLOW, "证据线索汇总（微缩）",
            {"result": "result", "original_data": "original_data"},
        ),
        entry.PROFILE_WORKFLOW: _child(
            entry.PROFILE_WORKFLOW, "人设特征判断（微缩）",
            {
                "profile_result": "profile_result",
                "hobby_result": "hobby_result",
                "style_result": "style_result",
            },
        ),
        entry.PRODUCTION_WORKFLOW: _child(
            entry.PRODUCTION_WORKFLOW, "画像生成&回写（微缩）",
            {"result1": "result1"},
        ),
    }


async def _run_entry(tmp_path, inputs: dict):
    settings = make_settings(tmp_path)
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
    # 真实的 tool 执行器 + 真实的嵌套调度器，只有子流程定义是微缩的
    register_tool_executor(registry, runtime, _children())
    scheduler = Scheduler(registry, runtime, max_active_nodes=8)
    return await scheduler.run(RunRequest(workflow=entry.WORKFLOW, inputs=inputs))


async def test_entry_runs_all_five_nodes(tmp_path):
    """主入口 5 个节点全部执行成功。"""
    outcome = await _run_entry(
        tmp_path, entry.default_inputs("13800000000", "batch-1")
    )
    assert outcome.succeeded, outcome.error
    assert len(outcome.node_executions) == 5
    assert all(e.succeeded for e in outcome.node_executions), [
        (e.node_id, e.status, e.error) for e in outcome.node_executions
    ]


async def test_entry_result1_comes_from_the_production_subworkflow(tmp_path):
    """``result1`` 由第三个子运行产出并原样透出，且是字符串。"""
    outcome = await _run_entry(tmp_path, entry.default_inputs("13800000000", ""))
    assert outcome.succeeded, outcome.error
    assert isinstance(outcome.outputs["result1"], str)
    assert outcome.outputs["result1"] == "13800000000"


async def test_each_tool_node_opens_a_nested_run(tmp_path):
    """三个 tool 节点各自触发一次子运行，``sub_run_id`` 互不相同。"""
    outcome = await _run_entry(tmp_path, entry.default_inputs("13800000000", ""))
    assert outcome.succeeded, outcome.error

    sub_runs = {
        node_id: outcome.node(node_id).sub_run_id
        for node_id in (entry.EVIDENCE, entry.PROFILE, entry.PRODUCTION)
    }
    assert all(sub_runs.values()), f"缺 sub_run_id：{sub_runs}"
    assert len(set(sub_runs.values())) == 3, f"子运行 ID 应当互不相同：{sub_runs}"
    assert set(outcome.sub_run_ids) == set(sub_runs.values())


def test_entry_inputs_flow_into_every_subworkflow():
    """三个子流程都能拿到 ``phone_number``——它由 start 一路透传。

    这条断言挡的是「子流程调用漏传入参」这类静默故障：父图拓扑照样成立、节点照样
    成功，但子流程拿着空号码跑出无关结果。
    """
    for node_id in (entry.EVIDENCE, entry.PROFILE, entry.PRODUCTION):
        node = entry.WORKFLOW.node_map[node_id]
        assert "phone_number" in node.config["@inputs"]
        assert node.config["@inputs"]["phone_number"] == "phone_number"


# ---------------------------------------------------------------- 真实链路（需 fixture）


FIXTURE = FIXTURE_DIR / "customer_profile_entry.json"
requires_entry_fixture = pytest.mark.skipif(
    not FIXTURE.is_file(),
    reason=(
        f"缺少 {FIXTURE}——需先运行 "
        "`python scripts/record_fixture.py --workflow customer_profile_entry` "
        "录制（脚本会脱敏，fixture 默认不入库）"
    ),
)


@requires_entry_fixture
async def test_entry_against_recorded_fixture(tmp_path):
    """用真实录制的 fixture 复跑主入口全链路。

    与上面的微缩子流程不同，这里**不替换任何子流程定义**——跑的是真实的 19 个工作流
    与全部提示词，只是外部响应来自录制。因此它能发现微缩图覆盖不到的接线错误。
    """
    from customer_profile.runner import build_service
    from customer_profile.workflows import load_all

    settings = make_settings(tmp_path)
    replay = ReplaySource.from_file(FIXTURE, strict=False)
    service = await build_service(settings, replay=replay)
    try:
        assert set(load_all()) >= {
            entry.EVIDENCE_WORKFLOW, entry.PROFILE_WORKFLOW, entry.PRODUCTION_WORKFLOW
        }
        outcome = await service.run(
            entry.WORKFLOW_ID, entry.default_inputs("13800000000", "")
        )
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    assert outcome.succeeded, outcome.error
    assert isinstance(outcome.outputs.get("result1"), str)
    # 子运行树完整：三个 tool 节点各自有一次嵌套运行
    assert len(outcome.sub_run_ids) == 3, (
        f"应有 3 次子运行，实际 {len(outcome.sub_run_ids)}：{outcome.sub_run_ids}"
    )

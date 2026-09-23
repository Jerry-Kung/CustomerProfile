"""``@body_template``：把含 Dify 引用的 JSON 文本还原成请求体。

这条路径此前**没有任何测试覆盖**——300 项测试全绿，而它实际是坏的：源码里写的是
``ctx.get(*parse_selector(...))``，而 ``ctx`` 是 ``RunContext``，没有 ``get``
方法（``get`` 属于 ``NodeResult``）。只有真实调用走到这一刻才会抛
``AttributeError: 'RunContext' object has no attribute 'get'``。

三个节点用它：``录音文件内容抽取`` 的 ``/submit_analyze`` 与 ``/query_analyze``，
以及 ``画像内容生成&回写`` 的回写体。因此这里按**引用位置**分别钉住两种 Dify 语义。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from customer_profile.definitions import NodeDef, WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

START = "bt_start"
CODE = "bt_code"
SUBMIT = "bt_submit"
END = "bt_end"


class _RecordingTransport(httpx.AsyncBaseTransport):
    """记录出网请求体。回放看不到「发出去的载荷」，因此这里直接拦请求。"""

    def __init__(self) -> None:
        self.bodies: list[object] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        self.bodies.append(json.loads(raw) if raw else None)
        return httpx.Response(200, json={"ok": True})


def _field_outputs(function: str, outputs: tuple[str, ...]) -> NodeDef:
    # 入参名 ``body`` 必须与 code 函数签名一致：执行器按关键字绑定（ctx.bind_inputs）
    return make_node(
        CODE,
        "产出字段",
        "code",
        after=(START,),
        inputs={"body": (START, "body")},
        outputs=outputs,
        function=function,
    )


def _workflow(*, body_template: str, code_function: str, outputs: tuple[str, ...]):
    return WorkflowDef(
        workflow_id="body_template_probe",
        display_name="请求体模板验证图",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"status": "status"},
        nodes=(
            make_node(START, "输入", "start", variables=("body",)),
            _field_outputs(code_function, outputs),
            make_node(
                SUBMIT,
                "提交",
                "http-request",
                after=(CODE,),
                outputs=("body", "status"),
                **{
                    # 本图验证的是 ``@body_template`` 的引用语义，与 AUC 契约无关。
                    # 这里**必须用一个非 auc 的服务名**：``service="auc"`` 会被
                    # ``HttpClient`` 转交给 ``AucClient``（云服务实现），那是一条
                    # 走真实协议、会自行轮询的路径，本图的 ``_RecordingTransport``
                    # 拦不到它，断言随即失效。
                    "@service": "profile",
                    "@method": "post",
                    "@path": "/submit_analyze",
                    "@body_template": body_template,
                    "@ssl_verify": False,
                    "@outputs": {"body": "$text", "status": "$status"},
                },
            ),
            make_node(
                END, "输出", "end", after=(SUBMIT,),
                inputs={"status": (SUBMIT, "status")}, outputs=("status",),
            ),
        ),
    )


async def _run(workflow: WorkflowDef, inputs: dict, tmp_path):
    settings = make_settings(tmp_path)
    transport = _RecordingTransport()
    limiter = asyncio.Semaphore(4)
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, request_limiter=limiter),
        http=HttpClient(settings, transport=transport, request_limiter=limiter),
        extra={},
    )
    scheduler = Scheduler(build_default_registry(), runtime, max_active_nodes=8)
    outcome = await scheduler.run(RunRequest(workflow=workflow, inputs=inputs))
    return outcome, transport


# 与 audio_content_extract 的「获取任务参数」同一形状：从上游字段取值
PARSE_FIELDS = """
import json

def main(body: str) -> dict:
    data = json.loads(body)
    return {"file_url": data["url"], "task_id": data["id"]}
"""


def _register(function_body: str, name: str) -> str:
    """把 code 正文注册进 registry，返回可引用的函数路径。

    用 ``register_code_function(name, callable)`` 而不是按名字登记源码字符串：
    登记表存的是**可调用对象**，动态导入那条路是给 ``module:attr`` 用的。
    """
    from customer_profile.execution import code_registry

    namespace: dict = {}
    exec(compile(function_body, f"<probe:{name}>", "exec"), namespace)
    code_registry.register_code_function(name, namespace["main"])
    return name


async def test_reference_not_inside_string_gets_quoted(tmp_path):
    """引用**不在字符串里**（``{ "file_url": {{#n.f#}} }``）：替换成带引号的字符串。"""
    path = _register(PARSE_FIELDS, "bt_test_parse")
    workflow = _workflow(
        body_template='{ "file_url": {{#bt_code.file_url#}} }',
        code_function=path,
        outputs=("file_url", "task_id"),
    )
    outcome, transport = await _run(workflow, {"body": '{"url": "http://x/a.mp3", "id": "t1"}'}, tmp_path)
    assert outcome.succeeded, outcome.error
    assert transport.bodies == [{"file_url": "http://x/a.mp3"}], (
        f"请求体应为 {{'file_url': '...'}}，实际 {transport.bodies!r}"
    )


async def test_reference_inside_string_is_escaped_not_double_quoted(tmp_path):
    """引用**已在字符串里**（``{"k": "{{#n.f#}}"}"``）：只转义，不重复加引号。

    若按「补引号」处理会产出 ``""值""`` 这种坏载荷，接口直接 400。
    """
    path = _register(PARSE_FIELDS, "bt_test_parse")
    workflow = _workflow(
        body_template='{"task_id": "{{#bt_code.task_id#}}"}',
        code_function=path,
        outputs=("file_url", "task_id"),
    )
    outcome, transport = await _run(workflow, {"body": '{"url": "u", "id": "t-9"}'}, tmp_path)
    assert outcome.succeeded, outcome.error
    assert transport.bodies == [{"task_id": "t-9"}], (
        f"字符串内的引用应只转义，实际 {transport.bodies!r}"
    )


async def test_all_three_body_template_nodes_use_resolvable_references():
    """真实定义的三个节点：模板里的每个引用都必须能在自己的图里解析。"""
    from customer_profile.workflows import audio_content_extract, customer_profile_production

    candidates = []
    for workflow in (audio_content_extract.WORKFLOW, customer_profile_production.WORKFLOW):
        for node in workflow.nodes:
            template = node.config.get("@body_template")
            if template:
                candidates.append((workflow, node, template))

    assert len(candidates) == 3, (
        f"应有 3 个 @body_template 节点，实际 {len(candidates)}："
        f"{[n.node_id for _, n, _ in candidates]}"
    )

    import re

    pattern = re.compile(r"\{\{#([^#{}]+)#\}\}")
    for workflow, node, template in candidates:
        refs = pattern.findall(template)
        assert refs, f"{node.node_id} 的模板没有引用，本用例失去意义"
        for ref in refs:
            source_id, _, field = ref.partition(".")
            producer = workflow.node_map.get(source_id)
            assert producer is not None, (
                f"{node.node_id} 的模板引用了 {ref}，但 {source_id} 不在本图里"
            )
            # ``start`` 节点的「产出」就是它声明的入参：下游引用 ``{{#<start>.field#}}``
            # 取的是工作流入参，因此这里把它计入可引用字段。
            available = set(producer.outputs)
            if producer.node_type == "start":
                available |= set(producer.config.get("variables") or ())
            assert field in available, (
                f"{node.node_id} 引用了 {ref}，但 {source_id} 只产出 {sorted(available)}"
            )

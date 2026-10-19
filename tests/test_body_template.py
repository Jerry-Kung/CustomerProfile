"""``@body_template``：把含节点引用的 JSON 文本还原成请求体。

这条路径此前**没有任何测试覆盖**——300 项测试全绿，而它实际是坏的：源码里写的是
``ctx.get(*parse_selector(...))``，而 ``ctx`` 是 ``RunContext``，没有 ``get``
方法（``get`` 属于 ``NodeResult``）。只有真实调用走到这一刻才会抛
``AttributeError: 'RunContext' object has no attribute 'get'``。

录音文件内容抽取的 ``/submit_analyze`` 与 ``/query_analyze`` 使用这条路径；
画像回写直接绑定 JSON 对象，由 ``test_m2m_writeback.py`` 验证。
这里按**引用位置**分别钉住两种引用语义。
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


async def test_audio_body_template_nodes_use_resolvable_references():
    """真实录音定义的两个模板：每个引用都必须能在自己的图里解析。"""
    from customer_profile.workflows import audio_content_extract, customer_profile_production

    candidates = []
    for workflow in (audio_content_extract.WORKFLOW, customer_profile_production.WORKFLOW):
        for node in workflow.nodes:
            template = node.config.get("@body_template")
            if template:
                candidates.append((workflow, node, template))

    assert len(candidates) == 2, (
        f"应有 2 个 @body_template 节点，实际 {len(candidates)}："
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


# ---------------------------------------------------------------- ``@body_ref``：整段取值

CALLBACK_AS_JSON = """
import json

def main(body: str) -> dict:
    payload = {
        "phone": "13500000000",
        "batch_id": "b-1",
        "analysis_result": {"profile_summary": {"name": "陈"}},
    }
    return {"callback_json": json.dumps(payload, ensure_ascii=False)}
"""

CALLBACK_AS_DICT = """
def main(body: str) -> dict:
    return {"callback_json": {
        "phone": "1",
        "batch_id": "",
        "analysis_result": {"profile_summary": {}},
    }}
"""

CALLBACK_NOT_JSON = """
def main(body: str) -> dict:
    return {"callback_json": "这不是 JSON"}
"""


def _ref_workflow(
    *, code_function: str, outputs: tuple[str, ...],
    bind: bool = True, declare: bool = True, also_template: bool = False,
) -> WorkflowDef:
    """``@body_ref`` 的验证图：请求体**整段取自** code 节点的产出。

    与 :func:`_workflow` 的分工：那个验证文本模板里的**引用语义**（该补引号还是只转义），
    本图验证**整段取值**——后者是唯一能产出「请求体里有一个对象字段」的走法，
    因为文本模板恒把引用渲染成带引号的字符串。

    ``declare`` 与 ``bind`` 分开是有意的：要验证「声明了却漏了绑定」，就得让配置标志为真
    而绑定为空。两者用同一个开关控制的话，那个用例验证的其实是「什么都没声明」，
    执行器会安安静静走默认路径——用例看着是通过的，前提却不成立。
    """
    config: dict = {
        "@service": "profile",
        "@method": "post",
        "@path": "/callback/update-profile",
        "@ssl_verify": False,
        "@outputs": {"body": "$text", "status": "$status"},
    }
    if declare:
        config["@body_ref"] = True
    if also_template:
        config["@body_template"] = '{"phone": {{#bt_code.callback_json#}}}'
    return WorkflowDef(
        workflow_id="body_ref_probe",
        display_name="请求体取值验证图",
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
                inputs={"@body_ref": (CODE, "callback_json")} if bind else {},
                outputs=("body", "status"),
                **config,
            ),
            make_node(
                END, "输出", "end", after=(SUBMIT,),
                inputs={"status": (SUBMIT, "status")}, outputs=("status",),
            ),
        ),
    )


def _error_text(outcome, node_id: str) -> str:
    """节点错误优先，其次才是运行级错误。"""
    node = outcome.node(node_id)
    return ((node.error if node else None) or outcome.error or "")


async def test_body_ref_parses_a_json_string_into_an_object_body(tmp_path):
    """``@body_ref`` 取到 JSON 串时要**解析成对象**，不能原样透传。

    这条用例钉住的正是这个能力存在的理由：契约要求 ``analysis_result`` 是**对象**，
    而 ``@body_template`` 恒把引用渲染成带引号的字符串，产不出对象。上游 ``code``
    节点的产出又恒是字符串，所以「解析」这一步不可省——省掉它，发出去的
    ``analysis_result`` 会是一个被引号包住的串，服务端只会回一个笼统的 4xx。
    """
    path = _register(CALLBACK_AS_JSON, "bt_test_callback_json")
    workflow = _ref_workflow(code_function=path, outputs=("callback_json",))
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert outcome.succeeded, outcome.error
    assert len(transport.bodies) == 1
    body = transport.bodies[0]
    assert isinstance(body, dict), f"请求体应为对象，实际 {type(body).__name__}"
    assert isinstance(body["analysis_result"], dict), "analysis_result 必须是对象"
    assert body["phone"] == "13500000000"
    assert body["analysis_result"]["profile_summary"] == {"name": "陈"}


async def test_body_ref_sends_a_dict_untouched(tmp_path):
    """值本来就是 dict 时原样发出，不做二次包装、不转成字符串。"""
    path = _register(CALLBACK_AS_DICT, "bt_test_callback_dict")
    workflow = _ref_workflow(code_function=path, outputs=("callback_json",))
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert outcome.succeeded, outcome.error
    assert transport.bodies == [
        {"phone": "1", "batch_id": "", "analysis_result": {"profile_summary": {}}}
    ]


async def test_body_ref_rejects_unparsable_value_without_sending(tmp_path):
    """取值不是合法 JSON 时报错，**不发坏载荷**。

    发出去一个坏载荷比让这次运行失败更糟：服务端可能回 200 并把一份坏数据落库。
    """
    path = _register(CALLBACK_NOT_JSON, "bt_test_callback_bad")
    workflow = _ref_workflow(code_function=path, outputs=("callback_json",))
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert not outcome.succeeded
    assert "不是合法 JSON" in _error_text(outcome, SUBMIT)
    assert transport.bodies == [], "解析失败时一个请求都不该发出"


async def test_body_ref_and_body_template_are_mutually_exclusive(tmp_path):
    """同时声明 ``@body_ref`` 与 ``@body_template`` 即报错，而不是悄悄挑一个。

    两者语义不同（整段取值 vs 文本替换），挑一个执行会让作者以为自己是另一种语义——
    这类「静默择一」是回写体形状出错最难查的来源。
    """
    path = _register(CALLBACK_AS_JSON, "bt_test_callback_conflict")
    workflow = _ref_workflow(
        code_function=path, outputs=("callback_json",), also_template=True
    )
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert not outcome.succeeded
    assert "互斥" in _error_text(outcome, SUBMIT)
    assert transport.bodies == []


async def test_body_ref_without_binding_is_rejected(tmp_path):
    """声明了 ``@body_ref`` 却没有对应绑定 → 报错，而不是退回去发空请求体。"""
    path = _register(CALLBACK_AS_JSON, "bt_test_callback_nobind")
    workflow = _ref_workflow(code_function=path, outputs=("callback_json",), bind=False)
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert not outcome.succeeded
    assert "没有对应绑定" in _error_text(outcome, SUBMIT)
    assert transport.bodies == []


# ---------------------------------------------------------------- 回写闸门（缺省零真实回传）

def _writeback_probe_workflow() -> WorkflowDef:
    """用**业务测试臂里那个回写节点的真实配置**造一张最小图。

    **为什么不直接跑整张臂**：那条链要从取数层开始，十几路真实调用，跑一次既不快也不
    该在单测里发生。这里把真实节点的 ``config`` 原样搬过来（``@writeback`` / ``@body_ref``
    / ``@path``），只把上游换成一个小 code 节点——**验证的正是「配置对不对」**，
    而不是「整条臂能不能跑」。

    真实节点若被改回 ``@body_template``、丢掉 ``@writeback``、或换了路径，这里会立刻红。
    """
    from customer_profile.workflows import customer_profile_pipeline as pipe

    real = {n.node_id: n for n in pipe.WORKFLOW.nodes}[pipe.CALLBACK_WRITE]
    path = _register(CALLBACK_AS_JSON, "bt_test_writeback_body")
    return WorkflowDef(
        workflow_id="writeback_probe",
        display_name="回写闸门验证图",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"status": "status"},
        nodes=(
            make_node(START, "输入", "start", variables=("body",)),
            _field_outputs(path, ("callback_json",)),
            make_node(
                SUBMIT,
                real.title,
                "http-request",
                after=(CODE,),
                inputs={"@body_ref": (CODE, "callback_json")},
                outputs=real.outputs,
                # 原样沿用真实节点的配置（不含绑定），这是本图存在的全部意义
                **{k: v for k, v in real.config.items()},
            ),
            make_node(
                END, "输出", "end", after=(SUBMIT,),
                inputs={"status": (SUBMIT, "status")}, outputs=("status",),
            ),
        ),
    )


async def test_writeback_node_skips_the_call_by_default(tmp_path):
    """缺省设置（``WRITEBACK_ENABLED=false``）下回写节点**不发请求**，只产出可解释结果。

    这是「调测阶段 API 回传接口仅预留代码、不做真实回传」这条要求在测试层面的落地。
    两件事一起断言：

    - **一个请求都没发出**（``transport.bodies`` 为空）——只看运行状态会误判，
      被跳过的运行**仍然显示 succeeded**；
    - 产出里的 ``body`` 明说「已跳过」，而不是一个假装成功的空响应，下游据此知道
      这次没有任何生产副作用。
    """
    workflow = _writeback_probe_workflow()
    outcome, transport = await _run(workflow, {"body": ""}, tmp_path)

    assert outcome.succeeded, outcome.error
    assert transport.bodies == [], "WRITEBACK_ENABLED=false 时不得发出任何回写请求"

    node = outcome.node(SUBMIT)
    assert node is not None
    body = json.loads(node.outputs["body"])
    assert body["writeback_skipped"] is True
    assert body["reason"] == "WRITEBACK_ENABLED=false"
    assert body["url"].endswith("callback/update-profile")
    assert node.outputs["status"] == 0


async def test_writeback_node_sends_when_the_switch_is_on(tmp_path):
    """开关打开时请求真的发出去，且请求体是**对象**（``analysis_result`` 不是字符串）。

    这一条是上面那条的反面：若两道闸门同时失效（``@writeback`` 丢了 + 请求体退化），
    只有这条能发现。它同时钉住 ``@body_ref`` 的解析语义在真实配置下仍然成立。
    """
    workflow = _writeback_probe_workflow()
    settings = make_settings(tmp_path, writeback_enabled=True)
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
    outcome = await scheduler.run(RunRequest(workflow=workflow, inputs={"body": ""}))

    assert outcome.succeeded, outcome.error
    assert len(transport.bodies) == 1
    sent = transport.bodies[0]
    assert isinstance(sent["analysis_result"], dict)
    assert sent["phone"] == "13500000000"

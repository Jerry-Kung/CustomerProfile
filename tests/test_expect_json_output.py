"""``expect_json`` 的 ``llm`` 节点：``text`` 必须是**字符串**，不是对象。

**这条用例的由来（2026-09-23 真实录音冒烟）。** 外呼录音工作流首次走到单文件路径后，
有效性判断的结果出现自相矛盾：

- ``request_attempts`` 里模型返回 ``{"is_valid": true, "reason": "双方通话…"}``；
- 下游 code 节点输出的却是 ``is_valid: false``。

根因是执行器把**解析后的 dict** 放进了 ``text``。两个消费方（``判断结果提取``、
``Notes结果拆分``）都是逐字符取自 DSL 的 code 节点，函数体里对 ``text`` 做
``json.loads``——传入 dict 会抛异常，而两者的 ``except`` 都会吞掉错误并按缺省值返回
（``is_valid=False`` / 空列表），于是**静默给出错误的业务结论**。

**为什么这个缺陷此前一直不可见。** 两个条件同时成立才会现形：

1. 该路径要真实输入才走到（另一个消费方 ``Notes结果拆分`` 在主入口链路上，但主入口
   从未在本机整跑过）；
2. 模型的判断必须与 ``except`` 的缺省值**相反**。此前的单方录音样本也判无效，错误与
   正确无从区分；真实双人通话才判出 ``true``，矛盾才暴露。

**为什么 337 项测试没抓到。** 没有任何用例真正执行过 ``execute_llm`` 的
``expect_json`` 分支：``test_llm_retry`` 只测 ``llm_call``（调用层返回 dict 是**对的**，
缺陷在执行器的输出映射），于是 ``json`` 未导入也能全绿。本文件补上这一段覆盖，
并把 ``text`` 与 ``parsed_json`` 的契约钉死。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler

from .conftest import make_settings

START = "ej_start"
LLM = "ej_llm"
END = "ej_end"

# 与真实场景同形：模型输出一个 JSON 对象
MODEL_JSON = {"is_valid": True, "reason": "录音为双方通话，存在有效沟通内容"}


class _JsonTransport(httpx.AsyncBaseTransport):
    """返回一个 JSON 对象作为模型回答。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(MODEL_JSON, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _workflow(*, expect_json: bool) -> WorkflowDef:
    return WorkflowDef(
        workflow_id="expect_json_probe",
        display_name="expect_json 输出契约验证图",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"text": "text"},
        nodes=(
            make_node(START, "输入", "start", variables=("prompt",)),
            make_node(
                LLM,
                "判断",
                "llm",
                after=(START,),
                inputs={"prompt": (START, "prompt")},
                outputs=("text",),
                # 固定系统提示，避免依赖模板资源
                system_text="你是一个有用的AI助手",
                expect_json=expect_json,
                original_node_id=LLM,
            ),
            make_node(
                END, "输出", "end", after=(LLM,),
                inputs={"text": (LLM, "text")}, outputs=("text",),
            ),
        ),
    )


async def _run(tmp_path, *, expect_json: bool):
    settings = make_settings(tmp_path)
    limiter = asyncio.Semaphore(4)
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, transport=_JsonTransport(), request_limiter=limiter),
        http=HttpClient(settings, transport=_JsonTransport(), request_limiter=limiter),
        extra={},
    )
    scheduler = Scheduler(build_default_registry(), runtime, max_active_nodes=4)
    return await scheduler.run(
        RunRequest(workflow=_workflow(expect_json=expect_json), inputs={"prompt": "请判断"})
    )


async def test_expect_json_text_is_a_json_string_not_a_dict(tmp_path):
    """``expect_json=True`` 时 ``text`` 必须是能被 ``json.loads`` 读回的字符串。

    这是 Dify 的契约：``llm`` 节点的输出一律是文本，下游 code 节点按文本消费。
    放 dict 进去会让它们的 ``except`` 吞掉错误并返回缺省值——静默的错误业务结论。
    """
    outcome = await _run(tmp_path, expect_json=True)
    assert outcome.succeeded, outcome.error

    text = outcome.outputs["text"]
    assert isinstance(text, str), (
        f"text 必须是字符串（下游 code 节点对它做 json.loads），实际 {type(text).__name__}"
    )
    assert json.loads(text) == MODEL_JSON, f"字符串应能读回原对象，实际 {text!r}"


async def test_expect_json_keeps_parsed_object_in_parsed_json(tmp_path):
    """解析后的对象保留在 ``parsed_json``，需要对象的场合仍可用。"""
    outcome = await _run(tmp_path, expect_json=True)
    assert outcome.succeeded, outcome.error

    node_result = outcome.node_executions
    llm_node = next(e for e in node_result if e.node_id == LLM)
    assert llm_node.outputs.get("parsed_json") == MODEL_JSON


async def test_plain_llm_text_is_unchanged(tmp_path):
    """``expect_json=False`` 时行为不变：``text`` 是模型返回的原文。"""
    outcome = await _run(tmp_path, expect_json=False)
    assert outcome.succeeded, outcome.error
    assert outcome.outputs["text"] == json.dumps(MODEL_JSON, ensure_ascii=False)


async def test_real_audio_validity_node_can_parse_the_text(tmp_path):
    """真实定义的消费方：``判断结果提取`` 对 ``text`` 取值必须得到正确的布尔。

    这条是把「契约」与「真实消费方」接起来：用修复后的形状喂给**逐字符取自 DSL 的**
    那个 code 节点，它必须能解析出 ``True`` 而不是因异常退化成 ``False``。
    """
    from customer_profile.workflows import shared_code

    outcome = await _run(tmp_path, expect_json=True)
    assert outcome.succeeded, outcome.error

    text = outcome.outputs["text"]
    # 与 audio_content_extract 的 code 节点同形：json.loads 后取 is_valid
    assert shared_code.extract_validity(text) == {"is_valid": True}


async def test_notes_split_node_can_parse_the_text(tmp_path):
    """另一个真实消费方 ``Notes结果拆分``（主入口链路）同样必须能解析。"""
    from customer_profile.workflows import customer_profile_production as cpp

    payload = {"problem_list": [{"a": 1}], "basic_notes_updates": [{"b": 2}]}
    text = json.dumps(payload, ensure_ascii=False)

    parsed = cpp.split_notes_result(text)
    assert json.loads(parsed["problem_list"]) == {"problem_list": [{"a": 1}]}
    assert json.loads(parsed["basic_notes_updates"]) == {"basic_notes_updates": [{"b": 2}]}

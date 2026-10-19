"""v3 P1 · 长 JSON 输出的可靠性：JSONL 逐行解析、括号抢救、节点级降级。

**这条用例的由来（v3 施工前的失败定案）。** v2.1 批次 ``13435844484`` 整案归零，
根因不是输出过长（``max_tokens=128000``、实际约 4000 completion tokens），而是
**深层嵌套下的括号配平失败**：三次尝试的报错位置**均等于输出总长度**，走查后**均余
2 个未闭合的 ``{``**（最外层 ``{"analysis_result":{``）。这是系统性缺陷，三试复现。

两条对策分别钉在这里：

- :func:`parse_jsonl_output`：一行坏只丢那一行，不丢整段；
- :func:`salvage_json_output`：严格解析失败时先补括号/截断，抢救到对象即算成功。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import (
    LlmClient,
    close_open_brackets,
    parse_jsonl_output,
    salvage_json_output,
)
from customer_profile.execution.scheduler import RunRequest, Scheduler

from .conftest import make_settings

# 与真实失败同形：内容完整，只有结尾少了闭合括号。
UNCLOSED = '{"analysis_result": {"a": [1, 2], "b": {"c": true}}}'


def test_close_open_brackets_appends_missing_closers():
    """结尾漏两个 ``}`` → 补齐后应能解析，且内容一分不改。"""
    broken = UNCLOSED[:-2]
    assert json.loads(close_open_brackets(broken)) == json.loads(UNCLOSED)


def test_close_open_brackets_leaves_well_formed_unchanged():
    assert json.loads(close_open_brackets(UNCLOSED)) == json.loads(UNCLOSED)


def test_salvage_recovers_truncated_missing_closers():
    """真实失败形态：内容写完、结尾漏括号 → 抢救得到对象。"""
    obj, reason = salvage_json_output(UNCLOSED[:-2])
    assert reason is None
    assert obj == json.loads(UNCLOSED)


def test_salvage_recovers_trailing_prose():
    """尾部多写了半句话 → 截到最后一个完整顶层对象。"""
    text = '{"a": 1, "b": 2} 以上是我的判断。'
    obj, reason = salvage_json_output(text)
    assert reason is None
    assert obj == {"a": 1, "b": 2}


def test_salvage_returns_none_for_hopeless_input():
    obj, reason = salvage_json_output("这根本不是 JSON")
    assert obj is None
    assert reason


def test_parse_jsonl_drops_only_the_bad_line():
    """坏行只丢那一行，其余照常解析——这是 JSONL 相对单个大 JSON 的核心优势。"""
    text = '\n'.join([
        '{"id": 1}',
        '{"id": 2',            # 坏行：缺闭合
        '{"id": 3}',
        '',
        '{"id": 4}',
    ])
    records, dropped = parse_jsonl_output(text)
    assert [r["id"] for r in records] == [1, 3, 4]
    assert len(dropped) == 1
    assert dropped[0]["line"] == 2


def test_parse_jsonl_ignores_blank_lines():
    records, dropped = parse_jsonl_output('\n\n{"a": 1}\n\n')
    assert records == [{"a": 1}]
    assert dropped == []


# ---------------------------------------------------------------- 执行器接线

class _TextTransport(httpx.AsyncBaseTransport):
    """返回一段固定文本作为模型回答。"""

    def __init__(self, content: str) -> None:
        self._content = content

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _workflow(*, output_format: str) -> WorkflowDef:
    start, llm, end = "jl_start", "jl_llm", "jl_end"
    return WorkflowDef(
        workflow_id="jsonl_probe",
        display_name="JSONL 输出契约验证图",
        source_dsl=None,
        entries=(start,),
        exits=(end,),
        outputs={"text": "text"},
        nodes=(
            make_node(start, "输入", "start", variables=("prompt",)),
            make_node(
                llm,
                "抽取",
                "llm",
                after=(start,),
                inputs={"prompt": (start, "prompt")},
                outputs=("text", "records", "dropped_lines", "parsed_json"),
                system_text="你是一个有用的AI助手",
                output_format=output_format,
            ),
            make_node(end, "输出", "end", after=(llm,),
                      inputs={"text": (llm, "text")}, outputs=("text",)),
        ),
    )


async def _run(tmp_path, *, output_format: str, content: str):
    settings = make_settings(tmp_path)
    limiter = asyncio.Semaphore(4)
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, transport=_TextTransport(content), request_limiter=limiter),
        http=HttpClient(settings, transport=_TextTransport(content), request_limiter=limiter),
        extra={},
    )
    scheduler = Scheduler(build_default_registry(), runtime, max_active_nodes=4)
    return await scheduler.run(
        RunRequest(workflow=_workflow(output_format=output_format), inputs={"prompt": "请抽取"})
    )


async def test_jsonl_node_keeps_records_and_reports_bad_lines(tmp_path):
    """``output_format=jsonl``：``records`` 只含好行，``dropped_lines`` 记坏行。"""
    content = '\n'.join(['{"id": 1}', '{"id": 2', '{"id": 3}'])
    outcome = await _run(tmp_path, output_format="jsonl", content=content)
    assert outcome.succeeded, outcome.error

    text = json.loads(outcome.outputs["text"])
    assert [r["id"] for r in text] == [1, 3]

    node = next(e for e in outcome.node_executions if e.node_id == "jl_llm")
    assert node.outputs["records"] == [{"id": 1}, {"id": 3}]
    assert len(node.outputs["dropped_lines"]) == 1


async def test_json_node_salvages_unclosed_output_instead_of_failing(tmp_path):
    """``output_format=json``：结尾漏括号的整案**不应归零**，应被抢救。"""
    outcome = await _run(tmp_path, output_format="json", content=UNCLOSED[:-2])
    assert outcome.succeeded, outcome.error
    assert json.loads(outcome.outputs["text"]) == json.loads(UNCLOSED)

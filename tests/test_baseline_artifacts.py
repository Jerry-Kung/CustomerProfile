"""留痕导出：递归、截断、原子落盘与中文编码。

装置与 ``test_entry_end_to_end.py`` 同思路——**真实的 tool 执行器 + 真实的嵌套调度器 +
真实的留痕**，只把子流程换成同形状的微缩图。导出管线要验的是「怎么把一棵运行树读出来」
与「怎么落盘」，与子流程内部的业务逻辑无关；照真图跑会要求 fixture 覆盖 14 路扇出与全部
提示词，体积与维护成本都不合理。

自造三层嵌套（父 → 子 → 孙）是刻意的：实测主入口是深度 3、18 个运行、250 条节点执行的
树，而只导根运行是最容易犯的错。三层能把「递归下去了」与「只导了一层」区分开。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from customer_profile.definitions import RunStatus, WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.execution.subworkflow import register_tool_executor
from customer_profile.persistence.store import Store
from customer_profile.persistence.tracker import RunTracker
from customer_profile.replay import ReplaySource
from customer_profile.workflows import customer_profile_entry as entry

from .conftest import REPO_ROOT, make_settings

import sys

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import baseline_artifacts as ba  # noqa: E402

CHILD_ID = "micro_child"
GRANDCHILD_ID = "micro_grandchild"

CHINESE_TEXT = "客户画像：偏好七座 SUV，关注续航与空间，用语正式但亲切。"
"""带中文的产出。编码 bug 在这里会暴露成乱码或 UnicodeEncodeError。"""


def _leaf(workflow_id: str, name: str, field_name: str, text: str) -> WorkflowDef:
    """叶子工作流：start → 模板节点 → end，产出一个字段。"""
    nodes = (
        make_node("c_start", "输入", "start", variables=("phone_number",)),
        make_node(
            "c_src",
            f"产出 {field_name}",
            "template-transform",
            after=("c_start",),
            inputs={"phone_number": ("c_start", "phone_number")},
            outputs=("output",),
            template_text=text,
        ),
        make_node(
            "c_end", "输出", "end", after=("c_src",),
            inputs={field_name: ("c_src", "output")}, outputs=(field_name,),
        ),
    )
    return WorkflowDef(
        workflow_id=workflow_id,
        display_name=name,
        source_dsl=None,
        entries=("c_start",),
        exits=("c_end",),
        outputs={field_name: "c_src"},
        nodes=nodes,
    )


def _parent(workflow_id: str, name: str, child_id: str, field_name: str) -> WorkflowDef:
    """父工作流：start → tool(调子流程) → end，把子流程产出原样透出。"""
    nodes = (
        make_node("p_start", "输入", "start", variables=("phone_number",)),
        make_node(
            "p_tool",
            f"调用 {child_id}",
            "tool",
            after=("p_start",),
            inputs={"phone_number": ("p_start", "phone_number")},
            outputs=(field_name,),
            **{"@workflow": child_id},
        ),
        make_node(
            "p_end", "输出", "end", after=("p_tool",),
            inputs={field_name: ("p_tool", field_name)}, outputs=(field_name,),
        ),
    )
    return WorkflowDef(
        workflow_id=workflow_id,
        display_name=name,
        source_dsl=None,
        entries=("p_start",),
        exits=("p_end",),
        outputs={field_name: "p_tool"},
        nodes=nodes,
    )


def _definitions() -> dict[str, WorkflowDef]:
    grandchild = _leaf(GRANDCHILD_ID, "孙流程（微缩）", "deep_value", CHINESE_TEXT)
    child = _parent(CHILD_ID, "子流程（微缩）", GRANDCHILD_ID, "deep_value")
    return {GRANDCHILD_ID: grandchild, CHILD_ID: child}


async def _build(tmp_path):
    """装配一个带留痕的调度器，返回 (scheduler, store, definitions)。

    ``NodeRuntime`` 必须带 ``recorder``：``SubWorkflowRunner`` 在没有 ``scheduler_factory``
    时按 ``runtime.recorder`` 新建子调度器，漏了这一步子运行照样跑通（父图 succeeded、
    子流程输出也透传），但**一次都不落库**——正是导出管线最该抓的那种静默故障。
    """
    settings = make_settings(tmp_path)
    limiter = asyncio.Semaphore(4)
    replay = ReplaySource(strict=False)
    store = Store(settings.sqlite_path)
    store.connect()
    tracker = RunTracker(store, is_replay=True, code_commit="test-commit")
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, replay=replay, request_limiter=limiter),
        http=HttpClient(settings, replay=replay, request_limiter=limiter),
        recorder=tracker,
        extra={},
    )
    definitions = _definitions()
    registry = build_default_registry()
    scheduler = Scheduler(registry, runtime, max_active_nodes=8, recorder=tracker)
    # register_tool_executor 自己构造 SubWorkflowRunner 并挂到 runtime 上；
    # 顺序要求是「先建好 scheduler/recorder」——子运行沿用父运行的留痕器。
    register_tool_executor(registry, runtime, definitions)
    return scheduler, store, definitions


async def _run_tree(tmp_path):
    """跑一次两层嵌套，返回根运行 ID 与 store。"""
    scheduler, store, _ = await _build(tmp_path)
    parent = _parent(entry.WORKFLOW_ID, "微缩主入口", CHILD_ID, "deep_value")
    outcome = await scheduler.run(
        RunRequest(
            workflow=parent,
            inputs={"phone_number": "13415100087", "batch_id": ""},
            business_ref="13415100087",
        )
    )
    assert outcome.succeeded, outcome.error
    return outcome.run_id, store, outcome


async def test_collect_walks_the_whole_tree(tmp_path):
    """导出必须递归到孙运行——只导根运行是这条管道最容易犯的错。"""
    root_run_id, store, outcome = await _run_tree(tmp_path)
    bundle = await ba.collect_run_tree(store, root_run_id, ba.ExportPolicy())

    workflow_ids = {item["workflow_id"] for item in bundle.tree}
    assert workflow_ids == {entry.WORKFLOW_ID, CHILD_ID, GRANDCHILD_ID}
    assert bundle.run_count == 3, [item["run_id"] for item in bundle.tree]
    assert bundle.observed_depth == 2
    assert bundle.missing_runs == []

    # 节点的 run_id 分布也要覆盖三层，否则说明节点是跟着某个 run 一起漏掉的
    node_runs = {item["run_id"] for item in bundle.nodes}
    assert node_runs == {item["run_id"] for item in bundle.tree}
    assert bundle.node_count == 9, bundle.node_count  # 每层 3 个节点
    assert set(outcome.sub_run_ids) <= {item["run_id"] for item in bundle.tree}


async def test_nodes_keep_call_path(tmp_path):
    """节点行必须带 call_path——迭代节点每轮重跑，只按 node_id 定位会混轮次。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    bundle = await ba.collect_run_tree(store, root_run_id, ba.ExportPolicy())
    assert all("call_path" in item for item in bundle.nodes)
    assert all("node_id" in item for item in bundle.nodes)
    # 与库里一致：条数与 (node_id, call_path) 联合键的去重数相同
    keys = {(item["run_id"], item["node_id"], item["call_path"]) for item in bundle.nodes}
    assert len(keys) == len(bundle.nodes)


async def test_result_payload_carries_outputs_and_definition(tmp_path):
    """result.json 的内容：最终输出原样、定义快照带提交号。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    bundle = await ba.collect_run_tree(store, root_run_id, ba.ExportPolicy())
    result = bundle.result

    assert result["run_id"] == root_run_id
    assert result["workflow_id"] == entry.WORKFLOW_ID
    assert result["business_ref"] == "13415100087"
    assert result["status"] == RunStatus.SUCCEEDED
    assert result["inputs"]["phone_number"] == "13415100087"
    assert result["outputs"]["deep_value"] == CHINESE_TEXT
    assert result["definition"]["code_commit"] == "test-commit"
    assert result["definition"]["definition_hash"]
    assert result["tree_summary"]["run_count"] == 3


async def test_export_writes_four_files(tmp_path):
    """四个产物文件都生成，且都是可解析的 UTF-8 JSON。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    target = tmp_path / "artifacts"
    bundle, written = await ba.export_run_tree(
        store, root_run_id, target, ba.ExportPolicy()
    )

    assert sorted(written) == sorted(ba.EXPORT_FILENAMES)
    assert all(size > 0 for size in written.values())
    for name in ba.EXPORT_FILENAMES:
        payload = json.loads((target / name).read_text(encoding="utf-8"))
        assert payload or isinstance(payload, list), name
    assert bundle.run_count == 3


async def test_exported_json_is_utf8_without_bom_and_roundtrips_chinese(tmp_path):
    """中文往返一致、无 BOM。CLAUDE.md 记过编码 bug，这里把它钉住。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    target = tmp_path / "artifacts"
    await ba.export_run_tree(store, root_run_id, target, ba.ExportPolicy())

    for name in ba.EXPORT_FILENAMES:
        raw = (target / name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        text = raw.decode("utf-8")  # 解码失败即测试失败
        json.loads(text)

    # 中文只出现在承载内容的两个文件里：tree.json 是运行索引、attempts.json 是出网记录，
    # 它们本来就不含节点输出，对它俩断言中文等于断言错的形状。
    result = json.loads((target / "result.json").read_text(encoding="utf-8"))
    assert result["outputs"]["deep_value"] == CHINESE_TEXT
    assert CHINESE_TEXT in (target / "result.json").read_text(encoding="utf-8")

    nodes = json.loads((target / "nodes.json").read_text(encoding="utf-8"))
    assert CHINESE_TEXT in (target / "nodes.json").read_text(encoding="utf-8")
    assert any(
        CHINESE_TEXT in json.dumps(item.get("outputs") or {}, ensure_ascii=False)
        for item in nodes
    ), "节点输出里应当能找到子流程产出的中文"


async def test_export_is_atomic_leaving_no_temp_files(tmp_path):
    """落盘后不留临时文件；有残留说明写入被中断。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    target = tmp_path / "artifacts"
    await ba.export_run_tree(store, root_run_id, target, ba.ExportPolicy())
    assert ba.leftover_temp_files(target) == []


# ---------------------------------------------------------------- 截断与摘要


def test_truncate_value_keeps_hash_of_original():
    """超限值被替换，但 sha256 对应**原文**——比对时靠它判断「变没变」。"""
    import hashlib

    big = "x" * 5000
    policy = ba.ExportPolicy(max_inline_bytes=1024)
    shrunk = ba.truncate_value(big, policy.max_inline_bytes)

    assert shrunk["_truncated"] is True
    assert shrunk["bytes"] == ba.serialized_size(big)
    payload = json.dumps(big, ensure_ascii=False, sort_keys=True)
    assert shrunk["sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert shrunk["preview"] == payload[: ba.PREVIEW_CHARS]
    assert len(shrunk["preview"]) == ba.PREVIEW_CHARS


def test_truncate_value_passes_small_values_through():
    """未超限的值原样保留——截断只该在真超限时发生。"""
    value = {"a": 1, "b": "短"}
    assert ba.truncate_value(value, 1024) == value


def test_shrink_mapping_truncates_per_value_not_wholesale():
    """逐值判断：一个大字段只摘掉自己，同级的其它小字段保持可读。"""
    payload = {"huge": "y" * 5000, "small": "ok"}
    shrunk = ba.shrink_mapping(payload, 1024)
    assert shrunk["huge"]["_truncated"] is True
    assert shrunk["small"] == "ok"


async def test_large_node_output_is_truncated_but_small_ones_survive(tmp_path):
    """端到端：超限节点输出被摘要，小输出保持原文，且摘要里带定位所需的键。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    big = "z" * 8000
    nodes = await store.list_node_executions(root_run_id)
    first = nodes[0]
    await store.upsert_node_execution(
        run_id=root_run_id,
        node_id=first["node_id"],
        node_title=first["node_title"] or first["node_id"],
        node_type=first["node_type"],
        call_path=first["call_path"],
        status=first["status"],
        inputs={"phone_number": "13415100087"},
        outputs={"blob": big, "tiny": "ok"},
    )

    bundle = await ba.collect_run_tree(
        store, root_run_id, ba.ExportPolicy(max_inline_bytes=1024)
    )
    target = next(
        item
        for item in bundle.nodes
        if item["run_id"] == root_run_id and item["node_id"] == first["node_id"]
    )
    assert target["outputs"]["blob"]["_truncated"] is True
    assert target["outputs"]["tiny"] == "ok"
    # 定位键齐全：需要原文时可凭它们回 trace.db 取
    assert target["node_id"] and target["call_path"] is not None and target["run_id"]


async def test_attempt_bodies_can_be_dropped(tmp_path):
    """``include_attempt_bodies=False`` 时请求体与响应只留摘要与哈希。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    await store.insert_attempt(
        run_id=root_run_id,
        node_id="n1",
        call_path="",
        attempt_no=1,
        kind="http",
        target="https://example.invalid/data/x",
        request={"prompt": "p" * 100},
        response_text="r" * 100,
        status_code=200,
        duration_ms=12,
    )

    full = await ba.collect_run_tree(store, root_run_id, ba.ExportPolicy())
    assert any(
        item.get("request_json") for item in full.attempts
    ), "缺省应保留请求体全文"

    lean = await ba.collect_run_tree(
        store, root_run_id, ba.ExportPolicy(include_attempt_bodies=False)
    )
    lean_attempt = next(
        item for item in lean.attempts if item.get("target", "").endswith("/data/x")
    )
    assert "request_json" not in lean_attempt
    assert "response_text" not in lean_attempt
    assert lean_attempt["request_digest"]["sha256"]
    assert lean_attempt["response_digest"]["sha256"]


async def test_missing_run_raises(tmp_path):
    """根运行不存在时报错，不返回一个空产物目录假装成功。"""
    _, store, _ = await _run_tree(tmp_path)
    with pytest.raises(KeyError):
        await ba.collect_run_tree(store, "nonexistent-run-id", ba.ExportPolicy())


async def test_depth_cap_is_reported_not_silent(tmp_path):
    """超出行深度上限时，把被截断的子运行记进 ``missing_runs``，不静默丢弃。"""
    root_run_id, store, _ = await _run_tree(tmp_path)
    bundle = await ba.collect_run_tree(
        store, root_run_id, ba.ExportPolicy(max_depth=0)
    )
    assert bundle.run_count == 1
    assert bundle.missing_runs, "截断必须留痕，否则「树很小」与「没递归」无法区分"

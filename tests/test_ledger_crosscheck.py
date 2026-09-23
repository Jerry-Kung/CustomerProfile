"""交叉核对：Python 定义与原 DSL 台账逐项一致。

V0.2 的验收标准之一是「对照 DSL 结构，节点/边/变量映射逐项一致」。本测试把这条
标准变成可执行的断言：拿 V0.1 生成的 ``node_ledger.json`` 当基线，逐工作流比对
节点 ID 集合、节点类型、直接前置、边的集合。

**只对 V0.2 已迁移的两个工作流断言。** 19 个工作流的全量比对属 V0.3，这里不假装
覆盖了还没迁移的部分。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.workflows import human_corrected_info, mengshi_it_system_data

from .conftest import REPO_ROOT, requires_dsl

LEDGER = REPO_ROOT / "docs" / "specs" / "ledger" / "node_ledger.json"

pytestmark = requires_dsl


@pytest.fixture(scope="module")
def ledger() -> dict:
    if not LEDGER.is_file():
        pytest.skip(f"缺少台账 {LEDGER}，请先运行 scripts/inventory/extract_ledger.py")
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _ledger_workflow(ledger: dict, display_name: str) -> dict:
    for workflow in ledger["workflows"]:
        if workflow["display_name"] == display_name:
            return workflow
    raise AssertionError(f"台账中没有工作流 {display_name}")


def _assert_matches(ledger_workflow: dict, definition) -> None:
    dsl_nodes = {str(n["node_id"]): n for n in ledger_workflow["nodes"]}
    py_nodes = {n.node_id: n for n in definition.nodes}

    assert set(py_nodes) == set(dsl_nodes), (
        f"{definition.workflow_id} 节点集合不一致："
        f"多出 {sorted(set(py_nodes) - set(dsl_nodes))}，"
        f"缺失 {sorted(set(dsl_nodes) - set(py_nodes))}"
    )

    for node_id, dsl_node in dsl_nodes.items():
        py_node = py_nodes[node_id]
        assert py_node.node_type == dsl_node["type"], f"{node_id} 类型不一致"
        assert set(py_node.predecessors) == set(dsl_node["direct_predecessors"]), (
            f"{node_id} 直接前置不一致"
        )

    dsl_edges = {
        (str(e["source"]), str(e["target"])) for e in ledger_workflow["edges"]
    }
    py_edges = {(e["source"], e["target"]) for e in definition.edge_list()}
    assert py_edges == dsl_edges, (
        f"{definition.workflow_id} 边集合不一致："
        f"多出 {sorted(py_edges - dsl_edges)}，缺失 {sorted(dsl_edges - py_edges)}"
    )


def test_mengshi_structure_matches_ledger(ledger):
    _assert_matches(
        _ledger_workflow(ledger, mengshi_it_system_data.DISPLAY_NAME),
        mengshi_it_system_data.WORKFLOW,
    )


def test_human_corrected_structure_matches_ledger(ledger):
    _assert_matches(
        _ledger_workflow(ledger, human_corrected_info.DISPLAY_NAME),
        human_corrected_info.WORKFLOW,
    )


def test_mengshi_llm_model_input_is_dropped(ledger):
    """有意差异 W4：``llm_model`` 入参按 §6.4.5 删除。台账里仍应有它，迁移后不应有。"""
    ledger_workflow = _ledger_workflow(ledger, mengshi_it_system_data.DISPLAY_NAME)
    start = next(n for n in ledger_workflow["nodes"] if n["type"] == "start")
    assert "llm_model" in start["literal_bindings"], (
        "台账里已没有 llm_model，说明基线变了，需要复核差异清单 W4"
    )

    migrated = mengshi_it_system_data.WORKFLOW.node_map[mengshi_it_system_data.START_NODE]
    assert "llm_model" not in migrated.config["variables"]
    assert "llm_model" not in mengshi_it_system_data.default_inputs("[]")


def test_mengshi_binding_sources_match_ledger(ledger):
    """code 节点的每条入参绑定来源必须与台账一致（变量映射，不只图结构）。"""
    ledger_workflow = _ledger_workflow(ledger, mengshi_it_system_data.DISPLAY_NAME)
    dsl_nodes = {str(n["node_id"]): n for n in ledger_workflow["nodes"]}

    for node_id in (mengshi_it_system_data.EXTRACT_NODE, mengshi_it_system_data.INTEGRATE_NODE):
        # 台账里 value_selector 是 JSON 数组，定义里是元组；都归一成二元组再比
        dsl_bindings = {
            key: (tuple(value[0]) if isinstance(value, list) else tuple(value))
            for key, value in dsl_nodes[node_id]["input_bindings"].items()
            if not key.startswith("@")
        }
        py_node = mengshi_it_system_data.WORKFLOW.node_map[node_id]
        py_bindings = {b.target: b.source for b in py_node.bindings}

        assert py_bindings == dsl_bindings, (
            f"{node_id} 入参绑定与台账不一致："
            f"迁移后 {py_bindings}，台账 {dsl_bindings}"
        )


def test_ledger_totals_unchanged(ledger):
    """台账本身未被改动：19 工作流 / 293 节点 / 324 边。任一变化都要复核 V0.1 结论。"""
    assert ledger["totals"]["workflows"] == 19
    assert ledger["totals"]["nodes"] == 293
    assert ledger["totals"]["edges"] == 324

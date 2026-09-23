"""交叉核对：Python 定义与原 DSL 台账逐项一致。

V0.2 的验收标准之一是「对照 DSL 结构，节点/边/变量映射逐项一致」。本测试把这条
标准变成可执行的断言：拿 V0.1 生成的 ``node_ledger.json`` 当基线，逐工作流比对
节点 ID 集合、节点类型、边的集合。

**V0.3 起覆盖全部已迁移工作流。** 有意差异（W1 / W2 / W4）会让结构与台账不同，
因此每个工作流可以声明自己的差异表；**未声明的差异一律判失败**——偏离必须是有记录
的决定，不允许悄悄漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.workflows import human_corrected_info, mengshi_it_system_data

from .conftest import REPO_ROOT, requires_dsl

LEDGER = REPO_ROOT / "docs" / "specs" / "ledger" / "node_ledger.json"

pytestmark = requires_dsl


# ====================================================================
# 有意差异表（W1 / W2 / W4）
#
# 每条都写清「删了什么、为什么」。这张表是**唯一**可以让结构偏离台账的地方，
# 也是唯一需要人工复核的东西：新增工作流时若发现这里是空的，说明没有差异，很好；
# 若报出未声明的差异，说明要么实现漏了，要么该在这里记一笔。
# ====================================================================

W2_W3_LLM_CALL = "有意差异 W2/W3：gemini_retry 工具节点归一为一次 llm_call"
W4_MODEL_BRANCH = "有意差异 W4：llm_model 分支按 §6.4.5 归一，只保留生产实际走的那一支"

DIFFERENCES: dict[str, dict] = {
    # W1：入口图删除冗余的第二个「画像内容生成&回写」节点（Q3 答复：不执行两次）
    "customer_profile_entry": {
        "removed_nodes": {
            "1777361789855": "有意差异 W1：入口图冗余节点（仅由 fail-branch 触发），Q3 决定只保留一个",
        },
        "removed_edges": [
            ("1777278748069", "1777361789855"),
            ("1777361789855", "1776765012890"),
        ],
        "added_edges": [],
        "retyped": {},
    },
    # W2/W3：工具节点归一为 llm 节点
    "chat_history_data": {
        "removed_nodes": {},
        "removed_edges": [],
        "added_edges": [],
        "retyped": {"1779672287769": ("tool", "llm", W2_W3_LLM_CALL)},
    },
    "user_feedback_data": {
        "removed_nodes": {},
        "removed_edges": [],
        "added_edges": [],
        "retyped": {"1779419296890": ("tool", "llm", W2_W3_LLM_CALL)},
    },
    "evidence_subagent": {
        "removed_nodes": {},
        "removed_edges": [],
        "added_edges": [],
        "retyped": {"1777366363196": ("tool", "llm", W2_W3_LLM_CALL)},
    },
    "customer_profile_production": {
        "removed_nodes": {},
        "removed_edges": [],
        "added_edges": [],
        "retyped": {
            node_id: ("tool", "llm", W2_W3_LLM_CALL)
            for node_id in (
                "1777368717878", "1777371252019", "1777371804681", "1777371986439",
                "1777372470283", "1777372801155", "1777373077830", "1777373257480",
                "1777373496170", "1777373760390", "1777374158895",
            )
        },
    },
    # W4：llm_model 入参删除 → 两个 if-else 归一。
    # 保留 **tool 支**：证据汇总图传的是字面量 "gemini"，生产环境走的正是 true 分支；
    # 且 tool 支的提示词多一条业务约束（社媒查询服务不稳定时不得推断「不使用该平台」），
    # 纯 llm 支没有。两支**不等价**，因此不能随便挑。
    "profile_features_analysis": {
        "removed_nodes": {
            "1776760124715": W4_MODEL_BRANCH,
            "17767603231790": W4_MODEL_BRANCH,
            "17767601866080": "属于被删除的纯 llm 支（提示词缺少社媒服务不稳定的业务约束）",
            "17767606554420": "属于被删除的纯 llm 支",
            "17767606912710": "属于被删除的纯 llm 支",
        },
        "removed_edges": [
            ("1776670030415", "1776760124715"),
            ("1776760124715", "17767601866080"),
            ("1776760124715", "1777427777301"),
            ("17767601866080", "1776760253638"),
            ("1776760253638", "17767603231790"),
            ("17767603231790", "17767606554420"),
            ("17767603231790", "17767606912710"),
            ("17767603231790", "1777427950125"),
            ("17767603231790", "1777427955726"),
            ("17767606554420", "1776760747042"),
            ("17767606912710", "1776760747042"),
        ],
        "added_edges": [
            # 分支删除后，原由分支串起来的顺序改用直接依赖表达
            ("1776670030415", "1777427777301"),
            ("1776760253638", "1777427950125"),
            ("1776760253638", "1777427955726"),
        ],
        "retyped": {
            "1777427788971": ("tool", "llm", W2_W3_LLM_CALL),
            "1777427931123": ("tool", "llm", W2_W3_LLM_CALL),
            "1777427938519": ("tool", "llm", W2_W3_LLM_CALL),
        },
    },
    # W4：录音类的 llm_model 分支废弃，删除该 if-else 与 gemini 侧 llm 节点。
    # 原值恒为 "gemini"，两支差异仅是模型选择，而模型已全项目统一（§6.4.4）。
    "test_drive_audio": {
        "removed_nodes": {
            "1776149288520": "gemini 侧 llm 节点（llm_model == gemini 那一支）",
            "1776763992027": W4_MODEL_BRANCH,
        },
        "removed_edges": [
            ("1776135353554", "1776763992027"),
            ("1776149288520", "1776764121079"),
            ("1776763992027", "1776149288520"),
            ("1776763992027", "17767640208110"),
        ],
        "added_edges": [
            ("17760451317570", "1776764121079"),
            ("1776135353554", "17767640208110"),
        ],
        "retyped": {},
    },
    "outbound_call_audio": {
        "removed_nodes": {
            "1776149288520": "gemini 侧 llm 节点（llm_model == gemini 那一支）",
            "1776764239626": W4_MODEL_BRANCH,
        },
        "removed_edges": [
            ("1776135353554", "1776764239626"),
            ("1776149288520", "1776764354171"),
            ("1776764239626", "1776149288520"),
            ("1776764239626", "17767643097270"),
        ],
        "added_edges": [
            ("17760451317570", "1776764354171"),
            ("1776135353554", "17767643097270"),
        ],
        "retyped": {},
    },
}


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
    """逐项比对，允许差异表里声明过的差异。

    ``direct_predecessors`` 不比：前置集合完全由边推导，边比过了再比前置是冗余检查，
    而 W4 的归一必然改动前置（上游直接连到保留那一支的首节点）。
    """
    diff = DIFFERENCES.get(definition.workflow_id, {})
    removed_nodes = diff.get("removed_nodes", {})
    retyped = diff.get("retyped", {})

    dsl_nodes = {str(n["node_id"]): n for n in ledger_workflow["nodes"]}
    py_nodes = {n.node_id: n for n in definition.nodes}

    expected_nodes = set(dsl_nodes) - set(removed_nodes)
    assert set(py_nodes) == expected_nodes, (
        f"{definition.workflow_id} 节点集合不一致："
        f"多出 {sorted(set(py_nodes) - expected_nodes)}，"
        f"缺失 {sorted(expected_nodes - set(py_nodes))}；"
        "若缺失属有意删除，请登记到 DIFFERENCES"
    )

    for node_id, py_node in py_nodes.items():
        dsl_type = dsl_nodes[node_id]["type"]
        if node_id in retyped:
            original, new_type, why = retyped[node_id]
            assert dsl_type == original, (
                f"{node_id} 的差异表声明原始类型 {original!r}，台账实际 {dsl_type!r}——"
                f"基线变了，需复核：{why}"
            )
            assert py_node.node_type == new_type, f"{node_id} 应为 {new_type}（{why}）"
            continue
        assert py_node.node_type == dsl_type, f"{node_id} 类型不一致"

    dsl_edges = {(str(e["source"]), str(e["target"])) for e in ledger_workflow["edges"]}
    removed_edges = {tuple(e) for e in diff.get("removed_edges", [])}
    added_edges = {tuple(e) for e in diff.get("added_edges", [])}
    py_edges = {(e["source"], e["target"]) for e in definition.edge_list()}

    assert py_edges - added_edges == dsl_edges - removed_edges, (
        f"{definition.workflow_id} 边集合不一致（已扣除声明的差异）："
        f"未声明的新增 {sorted((py_edges - added_edges) - (dsl_edges - removed_edges))}，"
        f"未声明的缺失 {sorted((dsl_edges - removed_edges) - (py_edges - added_edges))}"
    )


# ---------------------------------------------------------------- V0.2 的两个载体


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


# ---------------------------------------------------------------- V0.3 全量


def _migrated_definitions() -> list:
    from customer_profile.workflows import load_all

    return [
        wf for wf in load_all().values() if wf.source_dsl
    ]


MIGRATED = _migrated_definitions()


def test_eighteen_business_workflows_are_migrated():
    """19 个台账工作流里，18 个作为独立图迁移；第 19 个是 Gemini 重试子流程。

    ``Gemini（异常输出重试版）`` 按有意差异 W2 不再作为独立图存在——它的 17 处调用点
    与自身的 11 个节点都归一成了 ``llm_call()`` 里的重试逻辑。因此它**没有**、也不该有
    对应的 workflow 模块。这条断言把「18 = 19 − 1」这个算术钉住：将来若有人把某个工作流
    漏登记，或者凭空多出一个模块，都会在这里现形。
    """
    assert len(MIGRATED) == 18, (
        f"应由 18 个独立图带 source_dsl，实际 {len(MIGRATED)}："
        f"{sorted(w.workflow_id for w in MIGRATED)}"
    )


def test_gemini_retry_is_not_a_separate_workflow(ledger):
    """W2 的反向断言：Gemini 重试子流程确实没有独立图，而它的节点确实在台账里。

    两件事都要成立才说明归一没有丢东西：它不在迁移结果里（已归一），
    但它仍在台账里（原始 DSL 的确有这张图，V0.1 的盘点没错）。
    """
    ledger_workflow = _ledger_workflow(ledger, "Gemini（异常输出重试版）")
    assert len(ledger_workflow["nodes"]) == 11, (
        f"台账里 Gemini 子流程应有 11 个节点，实际 {len(ledger_workflow['nodes'])}——"
        "基线变了，需要复核差异清单 W2"
    )
    names = {w.display_name for w in MIGRATED}
    assert "Gemini（异常输出重试版）" not in names, (
        "Gemini 重试子流程不应作为独立工作流出现在迁移结果里（有意差异 W2）"
    )


@pytest.mark.parametrize("definition", MIGRATED, ids=[w.workflow_id for w in MIGRATED])
def test_structure_matches_ledger(ledger, definition):
    """每个工作流的节点集合、类型、边集合都与台账一致（扣除已声明的差异）。"""
    _assert_matches(_ledger_workflow(ledger, definition.display_name), definition)


@pytest.mark.parametrize("definition", MIGRATED, ids=[w.workflow_id for w in MIGRATED])
def test_no_undeclared_differences(ledger, definition):
    """反向检查：差异表里声明的每一项都真的发生了。

    差异表只增不减会慢慢失真——某天某个「有意删除」被恢复回来，前一条测试照样通过，
    而表里还留着那条记录。这里逐项确认声明仍然成立。
    """
    diff = DIFFERENCES.get(definition.workflow_id)
    if not diff:
        return

    ledger_workflow = _ledger_workflow(ledger, definition.display_name)
    dsl_nodes = {str(n["node_id"]) for n in ledger_workflow["nodes"]}
    py_nodes = {n.node_id for n in definition.nodes}
    dsl_edges = {(str(e["source"]), str(e["target"])) for e in ledger_workflow["edges"]}
    py_edges = {(e["source"], e["target"]) for e in definition.edge_list()}

    for node_id in diff.get("removed_nodes", {}):
        assert node_id in dsl_nodes, f"差异表声明的被删节点 {node_id} 不在台账里"
        assert node_id not in py_nodes, (
            f"差异表声明 {node_id} 被删除，但迁移后的定义里仍有它——请更新差异表"
        )

    for node_id, (original, new_type, _why) in diff.get("retyped", {}).items():
        assert node_id in py_nodes, f"差异表声明的改型节点 {node_id} 不在定义里"
        assert definition.node_map[node_id].node_type == new_type

    for edge in diff.get("removed_edges", []):
        assert tuple(edge) in dsl_edges, f"差异表声明的被删边 {edge} 不在台账里"
        assert tuple(edge) not in py_edges, f"差异表声明边 {edge} 被删除，但它还在"

    for edge in diff.get("added_edges", []):
        assert tuple(edge) in py_edges, f"差异表声明新增的边 {edge} 不在定义里"
        assert tuple(edge) not in dsl_edges, f"差异表声明新增的边 {edge} 其实台账里也有"


# ---------------------------------------------------------------- 入参与绑定


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
    """台账本身未被改动：19 工作流 / 293 节点 / 324 边。任一变化都要复核 V0.1 结论。

    这 293 个节点里，49 个是 ``code`` 节点（47 个随工作流迁移 + Gemini 子流程的 2 个，
    见 ``tests/test_code_verbatim.py``）。
    """
    assert ledger["totals"]["workflows"] == 19
    assert ledger["totals"]["nodes"] == 293
    assert ledger["totals"]["edges"] == 324

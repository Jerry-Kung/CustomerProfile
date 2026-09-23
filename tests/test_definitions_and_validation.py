"""定义层与校验的测试。

重点不是「函数能跑」，而是：拓扑能从定义直出、校验能真的报错。校验失效会让错误的图
在运行期以「节点未产出结果」的形式暴露，那时代价高得多。
"""

from __future__ import annotations

import pytest

from customer_profile.definitions import (
    Binding,
    NodeDef,
    WorkflowDef,
    make_node,
    parse_selector,
)
from customer_profile.validation import (
    DefinitionError,
    require_valid,
    validate,
)


def _simple_workflow(**overrides) -> WorkflowDef:
    nodes = (
        make_node("s", "输入", "start", variables=("x",)),
        make_node("a", "处理", "code", after=("s",), inputs={"x": ("s", "x")},
                  outputs=("y",), function="m:f"),
        make_node("e", "输出", "end", after=("a",), inputs={"result1": ("a", "y")},
                  outputs=("result1",)),
    )
    base = dict(
        workflow_id="wf",
        display_name="测试图",
        nodes=nodes,
        entries=("s",),
        exits=("e",),
        outputs={"result1": "result1"},
    )
    base.update(overrides)
    return WorkflowDef(**base)


# ---------------------------------------------------------------- 基础


def test_topological_order_respects_dependencies():
    workflow = _simple_workflow()
    order = workflow.structural_order()
    assert order.index("s") < order.index("a") < order.index("e")


def test_successors_are_inverted_predecessors():
    successors = _simple_workflow().successors()
    assert successors["s"] == {"a"}
    assert successors["a"] == {"e"}
    assert successors["e"] == set()


def test_edge_list_matches_declared_predecessors():
    workflow = _simple_workflow()
    # 按 (source, target) 排序后 'a' < 's'，因此 a→e 排在 s→a 之前
    # ``source_handle`` 保留 DSL 的分支名（普通边为 ``"source"``）：它是分支门槛
    # 的原始依据，丢了会让 if-else 的两支同时执行。
    assert sorted(workflow.edge_list(), key=lambda e: (e["source"], e["target"])) == [
        {"source": "a", "target": "e", "source_handle": "source"},
        {"source": "s", "target": "a", "source_handle": "source"},
    ]


def test_entry_resolution_prefers_declared_entry():
    workflow = _simple_workflow()
    assert workflow.resolve_entry() == "s"
    assert workflow.resolve_entry("a") == "a"


def test_entry_resolution_rejects_unknown():
    with pytest.raises(KeyError):
        _simple_workflow().resolve_entry("nope")


def test_entry_resolution_requires_unambiguous_implicit_entry():
    workflow = WorkflowDef(
        workflow_id="two_roots",
        display_name="两个入口",
        nodes=(
            make_node("s1", "输入1", "start"),
            make_node("s2", "输入2", "start"),
        ),
    )
    with pytest.raises(ValueError, match="入口不唯一"):
        workflow.resolve_entry()


def test_parse_selector_accepts_three_spellings():
    assert parse_selector("a.b") == ("a", "b")
    assert parse_selector(("a", "b")) == ("a", "b")
    assert parse_selector(["a", "b"]) == ("a", "b")


def test_parse_selector_rejects_malformed():
    with pytest.raises(ValueError):
        parse_selector("nodot")
    with pytest.raises(ValueError):
        parse_selector(("only-one",))


def test_node_requires_id_and_type():
    with pytest.raises(ValueError):
        NodeDef(node_id="", title="x", node_type="code")
    with pytest.raises(ValueError):
        NodeDef(node_id="x", title="x", node_type="")


# ---------------------------------------------------------------- normalise


def test_normalise_deduplicates_predecessors_and_bindings():
    node = NodeDef(
        node_id="a",
        title="处理",
        node_type="code",
        predecessors=("s", "s"),
        bindings=(
            Binding(target="x", source=("s", "x")),
            Binding(target="x", source=("s", "x")),
        ),
    )
    workflow = WorkflowDef(
        workflow_id="wf",
        display_name="去重",
        nodes=(make_node("s", "输入", "start"), node),
        entries=("s",),
    )
    cleaned = workflow.normalise()
    assert cleaned.node_map["a"].predecessors == ("s",)
    assert len(cleaned.node_map["a"].bindings) == 1


def test_normalise_orders_nodes_topologically():
    start = make_node("s", "输入", "start")
    first = make_node("a", "先", "code", after=("s",))
    second = make_node("b", "后", "code", after=("a",))
    workflow = WorkflowDef(
        workflow_id="wf",
        display_name="乱序",
        nodes=(second, first, start),
        entries=("s",),
    )
    assert [n.node_id for n in workflow.normalise().nodes] == ["s", "a", "b"]


def test_asdict_round_trips_topology():
    exported = _simple_workflow().asdict()
    assert exported["workflow_id"] == "wf"
    assert [n["node_id"] for n in exported["nodes"]] == ["s", "a", "e"]
    assert exported["edges"] == [
        {"source": "s", "target": "a", "source_handle": "source"},
        {"source": "a", "target": "e", "source_handle": "source"},
    ]


# ---------------------------------------------------------------- 校验


def test_valid_workflow_has_no_errors():
    issues = validate(_simple_workflow())
    assert [i for i in issues if i.level == "error"] == []


def test_validation_catches_duplicate_node_id():
    workflow = _simple_workflow(
        nodes=(
            make_node("s", "输入", "start"),
            make_node("s", "重复", "code", after=("s",)),
        ),
        entries=("s",),
        exits=(),
    )
    codes = {i.code for i in validate(workflow)}
    assert "duplicate_node_id" in codes


def test_validation_catches_unknown_predecessor():
    workflow = _simple_workflow(
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "处理", "code", after=("ghost",)),
        ),
        entries=("s",),
        exits=(),
    )
    codes = {i.code for i in validate(workflow)}
    assert "unknown_predecessor" in codes


def test_validation_catches_cycle():
    workflow = WorkflowDef(
        workflow_id="cyc",
        display_name="环",
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "甲", "code", after=("s", "b")),
            make_node("b", "乙", "code", after=("a",)),
        ),
        entries=("s",),
    )
    codes = {i.code for i in validate(workflow)}
    assert "cycle_detected" in codes


def test_validation_catches_binding_to_unknown_node():
    workflow = _simple_workflow(
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "处理", "code", after=("s",),
                      inputs={"x": ("ghost", "y")}, outputs=("y",)),
        ),
        entries=("s",),
        exits=(),
    )
    codes = {i.code for i in validate(workflow)}
    assert "unknown_binding_source" in codes


def test_validation_catches_binding_to_non_ancestor():
    """引用了存在但不是祖先的节点：报告不一致，不自动加边（§5.2）。"""
    workflow = WorkflowDef(
        workflow_id="nonancestor",
        display_name="非祖先",
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "甲", "code", after=("s",), outputs=("y",)),
            make_node("b", "乙", "code", after=("s",),
                      inputs={"x": ("a", "y")}, outputs=("z",)),
        ),
        entries=("s",),
    )
    codes = {i.code for i in validate(workflow)}
    assert "binding_source_not_ancestor" in codes


def test_validation_warns_on_undeclared_output_field():
    workflow = WorkflowDef(
        workflow_id="badfield",
        display_name="字段名不符",
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "甲", "code", after=("s",), outputs=("y",)),
            make_node("b", "乙", "code", after=("a",),
                      inputs={"x": ("a", "nope")}, outputs=("z",)),
        ),
        entries=("s",),
    )
    found = [i for i in validate(workflow) if i.code == "unknown_output_field"]
    assert found and all(i.level == "warning" for i in found)


def test_validation_catches_unreachable_node():
    workflow = WorkflowDef(
        workflow_id="orphan",
        display_name="孤岛",
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "处理", "code", after=("s",)),
            make_node("island", "孤岛节点", "code"),
        ),
        entries=("s",),
        exits=(),
    )
    codes = {i.code for i in validate(workflow)}
    assert "unreachable_nodes" in codes


def test_validation_reports_missing_start_and_end():
    workflow = WorkflowDef(
        workflow_id="empty",
        display_name="空图",
        nodes=(make_node("a", "甲", "code"),),
    )
    codes = {i.code for i in validate(workflow)}
    assert "no_start" in codes
    assert "no_end" in codes  # warning：不算 error


def test_require_valid_raises_with_all_errors_listed():
    workflow = WorkflowDef(
        workflow_id="broken",
        display_name="坏图",
        nodes=(
            make_node("s", "输入", "start"),
            make_node("a", "甲", "code", after=("ghost1",)),
            make_node("b", "乙", "code", after=("ghost2",)),
        ),
    )
    with pytest.raises(DefinitionError) as excinfo:
        require_valid(workflow)
    message = str(excinfo.value)
    assert "unknown_predecessor" in message
    assert "ghost1" in message and "ghost2" in message


# ---------------------------------------------------------------- 复合绑定

def test_make_node_marks_config_bindings():
    node = make_node(
        "h", "HTTP", "http-request", inputs={"@path": ("s", "phone"), "body": ("s", "raw")}
    )
    by_target = {b.target: b for b in node.bindings}
    assert by_target["@path"].is_config is True
    assert by_target["body"].is_config is False

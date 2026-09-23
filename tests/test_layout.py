"""拓扑布局测试。

布局最容易出的两类问题，各自钉住：

1. **分层算错**——尤其「扇出后汇合」的汇合点。用最短路径分层会把汇合点排到扇出点
   左边，连线反向；必须取最长路径。用一个人造图直接表达这个差别。
2. **遇环崩溃或死循环**——7 个截图类工作流的迭代节点确实带环，
   ``WorkflowDef.structural_order()`` 在这类图上会抛 ``CycleError``。布局不能走那条路。

另有两条不变式：结果覆盖全部节点、同一份定义两次调用结果逐点相同。
"""

from __future__ import annotations

from customer_profile.definitions import Binding, NodeDef, WorkflowDef
from customer_profile.layout import COLUMN_WIDTH, layout
from customer_profile.workflows import load_all

# 7 个截图类工作流 + 录音类，都是带迭代环的图。
CYCLIC_WORKFLOWS = (
    "moments_info",
    "wechat_homepage",
    "wechat_search_homepage",
    "xiaohongshu_homepage",
    "douyin_homepage",
    "alipay_homepage",
    "outbound_call_audio",
    "test_drive_audio",
)


def _graph(
    name: str, edges: dict[str, tuple[str, ...]], **kw: object
) -> WorkflowDef:
    """按 ``{节点: 前置元组}`` 构造一个最小定义。节点序即声明序。"""
    nodes = tuple(
        NodeDef(node_id=nid, title=nid, node_type="code", predecessors=preds)
        for nid, preds in edges.items()
    )
    return WorkflowDef(
        workflow_id=name, display_name=name, nodes=nodes, **kw  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- 分层


def test_nodes_without_predecessors_are_the_first_layer():
    wf = _graph("t", {"a": (), "b": (), "c": ("a",)})
    pos = layout(wf)
    assert pos["a"][0] == pos["b"][0]
    assert pos["c"][0] > pos["a"][0]


def test_layering_takes_the_longest_path_not_the_shortest():
    """汇合点必须排在**最长**前置路径之后。

    图的形状::

        a → b → c ─┐
        └──────────┴→ d

    ``d`` 的前置是 ``a``（0 层）与 ``c``（2 层）。最长路径给出 3 层，最短路径会给出 1 层
    ——后者把 ``d`` 排到 ``b`` 左边，连线反向。这是本测试要钉住的差别。
    """
    wf = _graph("t", {"a": (), "b": ("a",), "c": ("b",), "d": ("a", "c")})
    pos = layout(wf)
    assert pos["d"][0] > pos["c"][0], "汇合点排到了最长前置的左边"
    assert pos["d"][0] > pos["b"][0]


def test_diamond_layering_is_stable_regardless_of_declaration_order():
    """同一张图、声明序不同，层号（x）必须一致。"""
    forward = _graph("t", {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
    backward = _graph("t", {"d": ("b", "c"), "c": ("a",), "b": ("a",), "a": ()})
    assert {k: v[0] for k, v in layout(forward).items()} == {
        k: v[0] for k, v in layout(backward).items()
    }


def test_fanout_puts_receivers_in_one_layer():
    """一个节点扇出到多个同构子节点，它们应在同一层。"""
    wf = _graph("t", {"a": (), "b": ("a",), "c": ("a",), "d": ("a",)})
    xs = {layout(wf)[n][0] for n in ("b", "c", "d")}
    assert len(xs) == 1


# ---------------------------------------------------------------- 环


def test_cycle_does_not_hang_or_raise():
    """带环的图必须能出坐标，且不死循环。

    注意这**不是**在声明支持环：实测 20 个真实定义全部无环（见下面的环普查）。
    本用例只钉住「不会崩、不会挂」这条下限，不声明环上的层号有意义。
    """
    preds = {"a": (), "b": ("a",), "c": ("b",), "a": ("c",)}
    nodes = tuple(
        NodeDef(node_id=n, title=n, node_type="code", predecessors=preds[n])
        for n in ("a", "b", "c")
    )
    wf = WorkflowDef(workflow_id="t", display_name="t", nodes=nodes)
    pos = layout(wf)
    assert set(pos) == {"a", "b", "c"}


def test_chain_layers_are_exactly_consecutive():
    """无环直链必须落在**精确连续**的层上：0,1,2,3。

    断言精确层号而不是相对顺序：退化实现（例如环上的松弛把节点推到第 10 层）
    只要保持相对顺序就能通过相对断言，实测漏过一次。
    """
    order = ("start", "x", "y", "back")
    preds = {"start": (), "x": ("start",), "y": ("x",), "back": ("y",)}
    nodes = tuple(
        NodeDef(node_id=n, title=n, node_type="code", predecessors=preds[n])
        for n in order
    )
    wf = WorkflowDef(workflow_id="t", display_name="t", nodes=nodes)

    x_of = {k: v[0] for k, v in layout(wf).items()}
    assert x_of["start"] < x_of["x"] < x_of["y"] < x_of["back"]
    # 相邻层间距恰为一个 COLUMN_WIDTH
    step = COLUMN_WIDTH
    assert x_of["x"] - x_of["start"] == step
    assert x_of["y"] - x_of["x"] == step
    assert x_of["back"] - x_of["y"] == step


def test_no_real_workflow_is_cyclic():
    """环普查：20 个已迁移定义必须全部无环。

    这条断言存在的意义不是「确保无环」，而是**在环被引入时立刻失败**。布局按 DAG
    设计（见 ``layout._layers`` 的说明），若某天工作流定义引入环，层号会失真，
    而失真的图仍能渲染、仍能通过相对顺序断言——只有这里会直接失败。
    """
    registry = load_all()
    cyclic: list[str] = []
    for workflow_id, definition in registry.items():
        try:
            definition.structural_order()
        except Exception:  # graphlib.CycleError 及其子类
            cyclic.append(workflow_id)
    assert not cyclic, f"以下工作流引入了环，布局不再适用：{cyclic}"


def test_every_real_cyclic_workflow_lays_out():
    """7 个截图类与录音类工作流逐个跑一遍布局：不抛异常、覆盖全部节点。"""
    registry = load_all()
    checked = 0
    for workflow_id in CYCLIC_WORKFLOWS:
        definition = registry.get(workflow_id)
        if definition is None:
            continue
        positions = layout(definition)
        assert set(positions) == {n.node_id for n in definition.nodes}, workflow_id
        checked += 1
    assert checked, "没有任何带环工作流被检查到——测试失去了意义"


# ---------------------------------------------------------------- 不变式


def test_layout_covers_every_node_of_every_workflow():
    registry = load_all()
    for workflow_id, definition in registry.items():
        positions = layout(definition)
        assert set(positions) == {n.node_id for n in definition.nodes}, workflow_id


def test_layout_is_deterministic():
    """同一份定义两次布局结果逐点相同。

    不确定性通常来自对 set 迭代排序；层内排序若依赖 set 会时好时坏。
    """
    registry = load_all()
    for workflow_id, definition in registry.items():
        assert layout(definition) == layout(definition), workflow_id


# ---------------------------------------------------------------- 显式坐标优先


def test_explicit_coords_win_over_automatic_layout():
    """有人工坐标的节点不被自动布局覆盖。"""
    wf = WorkflowDef(
        workflow_id="t",
        display_name="t",
        nodes=(
            NodeDef("a", "a", "code", coords=(999.0, 888.0)),
            NodeDef("b", "b", "code", predecessors=("a",)),
        ),
    )
    pos = layout(wf)
    assert pos["a"] == (999.0, 888.0)


def test_workflow_without_any_coords_is_fully_automatic():
    wf = _graph("t", {"a": (), "b": ("a",)})
    assert all(n.coords is None for n in wf.nodes)
    pos = layout(wf)
    assert len(pos) == 2

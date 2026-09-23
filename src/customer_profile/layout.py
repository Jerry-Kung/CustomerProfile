"""拓扑布局：把工作流定义排成可供前端绘制的坐标。

**为什么需要这一层**：`NodeDef.coords` 字段为复用 Dify 的 ``position`` 预留，但迁移时
285 个节点只填了 22 个。用户 2026-09-23 决定放弃回填 DSL 坐标，改用自动布局——回填会产生
一份与 DSL 强相关的派生物，与「纯代码构建、与 Dify 解关联」的初衷相悖。

**为什么不用拓扑排序分层**：``WorkflowDef.structural_order()`` 基于
:class:`graphlib.TopologicalSorter`，遇环抛 :class:`CycleError`，而截图类工作流（7 个）
的迭代节点确实带环。本模块用最长路径分层，对环安全。

**为什么是纯函数**：布局结果不落库、不入定义，只在导出时按需计算。定义层因此保持
「一份事实来源」，前端拿到的是同一份定义加一份算出来的坐标。

**已知代价**：扇出图上会变宽。证据汇总图出度 14、画像生成图出度 7，分层算法会把它们
摊成很宽的一行。缓解手段见 :func:`_order_layer`。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .definitions import WorkflowDef

# 布局几何参数。取值只影响观感，不影响正确性；集中在此便于调整。
COLUMN_WIDTH = 320.0
"""相邻层的水平间距。"""

ROW_HEIGHT = 140.0
"""同层相邻节点的垂直间距（节点自身高度的估计值）。"""

ORIGIN_X = 80.0
ORIGIN_Y = 282.0
"""第一层第一个节点的起始位置。沿用入口图 DSL 的排版起点，与历史截图观感一致。"""

LAYOUT_MARGIN = 40.0
"""层内首尾留白。"""


def layout(definition: WorkflowDef) -> dict[str, tuple[float, float]]:
    """算出每个节点的展示坐标。

    - 已有 ``NodeDef.coords`` 的节点**优先用其值**，保留手工钉位置的能力；
    - 其余节点按最长路径分层：层号决定 x，层内顺序决定 y。

    返回 ``{node_id: (x, y)}``，覆盖定义中的**全部**节点。同一个定义两次调用结果相同
    （层内排序按节点在定义中的声明序，不依赖 dict/set 的迭代顺序）。
    """
    layers = _layers(definition)
    declared_order = {n.node_id: i for i, n in enumerate(definition.nodes)}

    positions: dict[str, tuple[float, float]] = {}
    fanout = _fanout(definition)

    for layer_no in sorted(layers):
        members = layers[layer_no]
        # 层内排序：先按「该节点的出度」降序，把扇出点排到层中央上方，让连线尽量少交叉；
        # 相同时退回声明序，保证结果稳定。
        ordered = sorted(
            members,
            key=lambda nid: (-fanout.get(nid, 0), declared_order.get(nid, 0)),
        )
        count = len(ordered)
        for row, node_id in enumerate(ordered):
            x = ORIGIN_X + layer_no * COLUMN_WIDTH
            y = ORIGIN_Y + (row - (count - 1) / 2.0) * ROW_HEIGHT
            positions[node_id] = (x, y)

    # 显式坐标优先于自动布局的结果。
    for node in definition.nodes:
        if node.coords:
            positions[node.node_id] = (float(node.coords[0]), float(node.coords[1]))

    return positions


def _layers(definition: WorkflowDef) -> dict[int, list[str]]:
    """按最长路径给每个节点分层。

    层的定义：无前置者（或前置全部不在本图内者）为第 0 层；其余节点为
    ``max(前置层号) + 1``。**必须取最长路径**——取最短会让「扇出后汇合」的汇合点
    排到扇出点左边，连线反向。

    算法用有界松弛（Bellman-Ford 形式的最长路径），轮数上限为 ``n-1``。

    关于环：实测 20 个工作流定义**全部无环**（见 ``tests/test_layout.py`` 的环普查），
    因此本函数按 DAG 设计，不为环做特殊语义。若日后引入环，松弛会在 ``n-1`` 轮后停止，
    得到的层号是**有限但无意义的**（环上节点被推到很右边），图会难看但不会崩、不会
    死循环。这是刻意的取舍：与其为不会出现的情形写断环逻辑（曾按 DFS 回边实现过一版，
    实测把链中间切断、导致环上节点错位），不如让环普查测试直接失败，逼出真实原因。
    """
    node_map = definition.node_map
    known = set(node_map)

    # 只保留指向本图内节点的前置。跨图引用（子流程入参）不产生布局依赖。
    preds: dict[str, list[str]] = {
        n.node_id: [p for p in n.predecessors if p in known] for n in definition.nodes
    }

    depth: dict[str, int] = {n.node_id: 0 for n in definition.nodes}
    # DAG 上最长路径不超过 n-1 条边。跑满 n-1 轮即收敛；带环时到此为止，不无限推高。
    for _ in range(max(0, len(definition.nodes) - 1)):
        changed = False
        for node_id in depth:
            parents = preds.get(node_id, ())
            if not parents:
                continue
            candidate = max(depth[p] for p in parents) + 1
            if candidate > depth[node_id]:
                depth[node_id] = candidate
                changed = True
        if not changed:
            break

    layers: dict[int, list[str]] = {}
    for node_id, level in depth.items():
        layers.setdefault(level, []).append(node_id)
    return layers


def _fanout(definition: WorkflowDef) -> dict[str, int]:
    """每个节点的出度，用于层内排序。"""
    counts: dict[str, int] = {n.node_id: 0 for n in definition.nodes}
    known = set(counts)
    for node in definition.nodes:
        for pred in node.predecessors:
            if pred in known:
                counts[pred] += 1
    return counts


def attach_layout(definition_dict: Mapping[str, object], definition: WorkflowDef) -> dict:
    """给 ``WorkflowDef.asdict()`` 的结果附加 ``layout`` 字段，供前端直接使用。

    ``asdict()`` 里的 ``coords`` 保持原样（原 DSL 坐标或 ``None``），布局结果放在独立的
    ``layout`` 字段：一个是**声明**，一个是**算出来的展示坐标**，混在一起会让
    「这个位置是人定的还是算出来的」无从分辨。
    """
    result = dict(definition_dict)
    result["layout"] = {
        node_id: [round(x, 2), round(y, 2)]
        for node_id, (x, y) in layout(definition).items()
    }
    return result


__all__ = ["layout", "attach_layout", "COLUMN_WIDTH", "ROW_HEIGHT"]

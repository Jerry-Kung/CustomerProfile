"""工作流定义层：节点、边、输入绑定的显式声明。

这是运行时的唯一事实来源。前端与执行器从同一份定义取拓扑，不维护第二张图
（见 `Dify迁移任务说明.md` §5.1）。

Dify 特有的两套变量机制在这里被区分开：

- ``{{#node_id.field#}}`` 引用 → :class:`Binding`，由调度层按依赖解析；
- 模板内部的 ``{{ variable }}`` 渲染 → 模板渲染函数的职责，不在此层。
"""

from __future__ import annotations

import graphlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------- 状态


class RunStatus:
    """工作流运行状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"

    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, SKIPPED, INTERRUPTED})


class NodeStatus:
    """节点执行状态。``BLOCKED`` 表示前置失败导致未执行。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"

    TERMINAL = frozenset({SUCCEEDED, FAILED, SKIPPED, BLOCKED})


# ---------------------------------------------------------------- 绑定

Selector = tuple[str, str]
"""变量选择器：``(node_id, field)``，对应 Dify 的 ``value_selector``。"""


@dataclass(frozen=True, slots=True)
class Binding:
    """一个入参到上游节点输出的绑定。

    ``@code`` / ``@url`` 这类以 ``@`` 开头的键承载配置项而非变量引用（V0.1 台账沿用同一约定），
    它们不产生数据依赖，但其中的引用仍须解析。
    """

    target: str
    """目标参数名。``@`` 前缀表示配置项。"""

    source: Selector
    """上游 ``(node_id, field)``。"""

    value_type: str = "string"
    """Dify 声明的类型，用于还原 JSON 字符串与对象的区别（不得任意互换）。"""

    is_config: bool = False
    """是否配置项（``@`` 前缀）。"""

    def asdict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "source": list(self.source),
            "value_type": self.value_type,
            "is_config": self.is_config,
        }


@dataclass(slots=True)
class NodeDef:
    """一个节点的最小声明：稳定 ID、类型、前置、输入绑定、执行配置、坐标。"""

    node_id: str
    """原 Dify 节点 ID，迁移后仍是稳定标识。"""

    title: str
    """显示名，保留原中文标题以便与 Dify 对照。"""

    node_type: str
    """节点类型：``start`` / ``end`` / ``code`` / ``template-transform`` /
    ``if-else`` / ``llm`` / ``http-request`` / ``tool`` / ``iteration`` 等。"""

    predecessors: tuple[str, ...] = ()
    """直接前置节点 ID。承载数据依赖与纯执行顺序依赖两类边。"""

    branch_from: tuple[str, str] | None = None
    """本节点只在某个 ``if-else`` 前置的**指定分支**上执行。

    形如 ``(if_else_node_id, "true")`` 或 ``(..., "false")``。``None`` 表示无条件执行。

    DSL 的边用 ``sourceHandle`` 区分分支（实测 55 条分支边），而迁移后的
    ``after=`` 只记了「有这条边」、丢了「走哪一支」。不还原这一位，两条分支会**同时执行**：
    截图类流程里表现为「文件不存在」与「文件存在」两条路一起跑，后者对着空串解析 JSON
    直接抛 :class:`JSONDecodeError`（V0.3 真实冒烟时实测到）。
    """

    branch_gates: tuple[tuple[str, str], ...] = ()
    """前置节点 ID → 必须走到的分支名。

    与 :attr:`branch_from` 的区别：那个只描述**唯一**那个分支前置，本字段是多条前置各带
    自己的门槛。调度层按本字段判定「这条边会不会来」，不会来就把该边标为失效。
    """

    bindings: tuple[Binding, ...] = ()
    """输入绑定。"""

    outputs: tuple[str, ...] = ()
    """该节点产出的字段名，供下游解析与前端展示。"""

    config: dict[str, Any] = field(default_factory=dict)
    """节点专属配置（HTTP 方法/路径、模板文件名、模型参数等）。"""

    coords: tuple[float, float] | None = None
    """原 DSL 的 position，作为第一版布局直接复用。"""

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("节点必须有 node_id")
        if not self.node_type:
            raise ValueError(f"节点 {self.node_id} 缺少 node_type")

    @property
    def branch_gates_map(self) -> dict[str, str]:
        """``{前置节点 ID: 分支名}``。普通前置不在其中（视为无条件）。"""
        return {k: v for k, v in self.branch_gates}

    def asdict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "type": self.node_type,
            "direct_predecessors": list(self.predecessors),
            "branch_gates": self.branch_gates_map,
            "bindings": [b.asdict() for b in self.bindings],
            "outputs": list(self.outputs),
            "config": self.config,
            "coords": list(self.coords) if self.coords else None,
        }


@dataclass(slots=True)
class WorkflowDef:
    """一个工作流的完整定义。"""

    workflow_id: str
    """稳定标识，例如 ``mengshi_it_system_data``。"""

    display_name: str
    """中文显示名，与 DSL 文件名对应。"""

    nodes: tuple[NodeDef, ...]
    """全部节点，顺序不限。"""

    entries: tuple[str, ...] = ()
    """入口节点 ID（``start`` 类型）。"""

    exits: tuple[str, ...] = ()
    """出口节点 ID（``end`` 类型）。"""

    source_dsl: str | None = None
    """来源 DSL 文件名，用于追溯。``None`` 表示人工构造的验证用图。"""

    outputs: Mapping[str, str] = field(default_factory=dict)
    """对外输出字段 → 产出它的节点 ID。约定最终契约是 ``result1`` 字符串。"""

    @property
    def node_map(self) -> dict[str, NodeDef]:
        return {n.node_id: n for n in self.nodes}

    def branch_gate_map(self) -> dict[str, dict[str, str]]:
        """节点 ID → {前置节点 ID: 必须走到的分支名}。供调度层判定边是否失效。"""
        return {n.node_id: n.branch_gates_map for n in self.nodes if n.branch_gates}

    def successors(self) -> dict[str, set[str]]:
        succ: dict[str, set[str]] = {n.node_id: set() for n in self.nodes}
        for node in self.nodes:
            for pred in node.predecessors:
                succ.setdefault(pred, set()).add(node.node_id)
        return succ

    def dependency_graph(self) -> dict[str, set[str]]:
        """``graphlib`` 需要的 ``{node: {依赖}}`` 形式。只含已声明的节点。"""
        known = {n.node_id for n in self.nodes}
        return {
            n.node_id: {p for p in n.predecessors if p in known} for n in self.nodes
        }

    def structural_order(self) -> list[str]:
        """按拓扑排序返回节点 ID。有环时抛 :class:`graphlib.CycleError`。"""
        return list(graphlib.TopologicalSorter(self.dependency_graph()).static_order())

    def resolve_entry(self, explicit: str | None = None) -> str:
        """判定工作流入口节点。"""
        if explicit:
            if explicit not in self.node_map:
                raise KeyError(f"指定的入口节点 {explicit} 不在定义中")
            return explicit
        if self.entries:
            return self.entries[0]
        zero_indegree = [n.node_id for n in self.nodes if not n.predecessors]
        if len(zero_indegree) == 1:
            return zero_indegree[0]
        raise ValueError(
            f"工作流 {self.workflow_id} 入口不唯一且未声明 entries：{zero_indegree}"
        )

    def edge_list(self) -> list[dict[str, str]]:
        """扁平边表，语义与 DSL 的 ``graph.edges`` 一致。

        ``source_handle`` 保留分支名（``true`` / ``false`` / ``fail-branch``），
        与台账 ``node_ledger.json`` 的字段同名同义；普通边为 ``"source"``。
        """
        return [
            {
                "source": pred,
                "target": node.node_id,
                "source_handle": node.branch_gates_map.get(pred, "source"),
            }
            for node in self.nodes
            for pred in node.predecessors
        ]

    def asdict(self) -> dict[str, Any]:
        """导出为可序列化结构，供只读前端使用（V0.4）。"""
        return {
            "workflow_id": self.workflow_id,
            "display_name": self.display_name,
            "source_dsl": self.source_dsl,
            "entries": list(self.entries),
            "exits": list(self.exits),
            "outputs": dict(self.outputs),
            "nodes": [n.asdict() for n in self.nodes],
            "edges": self.edge_list(),
        }

    def normalise(self) -> "WorkflowDef":
        """一致性整理：去重前置与绑定，并把节点按拓扑序排列。

        定义是人写的，重复项属于笔误；这里收敛掉，而不是让调度层去容忍。
        """
        order = {node_id: i for i, node_id in enumerate(self.dependency_order_safe())}
        cleaned: list[NodeDef] = []
        for node in self.nodes:
            seen: set[str] = set()
            preds: list[str] = []
            for p in node.predecessors:
                if p not in seen:
                    seen.add(p)
                    preds.append(p)
            preds.sort(key=lambda p: order.get(p, 0))

            bseen: set[tuple[str, Selector]] = set()
            binds: list[Binding] = []
            for b in node.bindings:
                key = (b.target, b.source)
                if key not in bseen:
                    bseen.add(key)
                    binds.append(b)

            cleaned.append(
                NodeDef(
                    node_id=node.node_id,
                    title=node.title,
                    node_type=node.node_type,
                    predecessors=tuple(preds),
                    bindings=tuple(binds),
                    outputs=node.outputs,
                    config=dict(node.config),
                    coords=node.coords,
                    branch_gates=tuple(node.branch_gates),
                )
            )
        cleaned.sort(key=lambda n: order.get(n.node_id, 0))
        return WorkflowDef(
            workflow_id=self.workflow_id,
            display_name=self.display_name,
            nodes=tuple(cleaned),
            entries=tuple(dict.fromkeys(self.entries)),
            exits=tuple(dict.fromkeys(self.exits)),
            source_dsl=self.source_dsl,
            outputs=dict(self.outputs),
        )

    def dependency_order_safe(self) -> list[str]:
        """拓扑序；遇到环时退化为声明顺序，供整理阶段使用。"""
        try:
            return self.structural_order()
        except graphlib.CycleError:
            return [n.node_id for n in self.nodes]


def make_node(
    node_id: str,
    title: str,
    node_type: str,
    *,
    after: Iterable[str] = (),
    inputs: Mapping[str, Selector | str | Sequence[str]] | None = None,
    outputs: Iterable[str] = (),
    coords: tuple[float, float] | None = None,
    on_branch: tuple[str, str] | None = None,
    **config: Any,
) -> NodeDef:
    """构造节点的简写。``inputs`` 的值可以是 ``(node_id, field)`` 或 ``'node_id.field'``。

    ``on_branch`` 声明「只在某个 ``if-else`` 的某一支上执行」，形如
    ``on_branch=("1775703027681", "true")``（DSL 的 ``sourceHandle``）。
    """
    bindings: list[Binding] = []
    for target, selector in (inputs or {}).items():
        bindings.append(
            Binding(
                target=target,
                source=parse_selector(selector),
                is_config=target.startswith("@"),
            )
        )
    gates: tuple[tuple[str, str], ...] = ()
    if on_branch is not None:
        gates = ((on_branch[0], on_branch[1]),)
    return NodeDef(
        node_id=node_id,
        title=title,
        node_type=node_type,
        predecessors=tuple(after),
        bindings=tuple(bindings),
        outputs=tuple(outputs),
        config=dict(config),
        coords=coords,
        branch_from=on_branch,
        branch_gates=gates,
    )


def parse_selector(value: Selector | str | Sequence[str]) -> Selector:
    """把多种写法归一到 ``(node_id, field)``。

    接受 ``('1776', 'result')``、``'1776.result'``、``['1776', 'result']``。
    节点 ID 与字段名都不含点，因此按第一个点切分无歧义。
    """
    if isinstance(value, str):
        node_id, _, field_name = value.partition(".")
        if not node_id or not field_name:
            raise ValueError(f"变量引用格式应为 'node_id.field'，实际为 {value!r}")
        return (node_id, field_name)
    parts = tuple(value)
    if len(parts) != 2:
        raise ValueError(f"变量引用应为二元组 (node_id, field)，实际为 {value!r}")
    return (parts[0], parts[1])

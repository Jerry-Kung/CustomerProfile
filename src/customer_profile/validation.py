"""定义层静态校验。

在开图之前把定义中的错误全部报出来。校验项对应 `Dify迁移任务说明.md` §5.2 的启动前校验：
节点 ID 唯一、边合法、无环、入口可达、变量生产者存在且路径有效。

**不做的事**：当变量引用的来源不在祖先链中时，只报告不一致，不自动加边改变原图。
"""

from __future__ import annotations

import graphlib
from dataclasses import dataclass
from typing import Iterable

from .definitions import NodeDef, WorkflowDef


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条校验发现。``level`` 为 ``error`` 时不允许开图。"""

    code: str
    message: str
    node_id: str | None = None
    level: str = "error"

    def __str__(self) -> str:
        where = f"[{self.node_id}] " if self.node_id else ""
        return f"{self.level}: {self.code}: {where}{self.message}"


class DefinitionError(Exception):
    """定义校验失败。``issues`` 含全部错误，便于一次性修完。"""

    def __init__(self, workflow_id: str, issues: Iterable[ValidationIssue]) -> None:
        self.workflow_id = workflow_id
        self.issues = list(issues)
        detail = "\n".join(f"  - {i}" for i in self.issues)
        super().__init__(f"工作流 {workflow_id} 定义校验未通过：\n{detail}")


def validate(workflow: WorkflowDef) -> list[ValidationIssue]:
    """返回全部校验发现（含警告）。空列表表示可以开图。"""
    issues: list[ValidationIssue] = []
    issues.extend(_check_unique_ids(workflow))
    issues.extend(_check_predecessors_exist(workflow))
    issues.extend(_check_acyclic(workflow))
    issues.extend(_check_bindings(workflow))
    issues.extend(_check_entry_reachable(workflow))
    issues.extend(_check_terminals(workflow))
    return issues


def require_valid(workflow: WorkflowDef) -> WorkflowDef:
    """校验并返回该定义；有 error 级发现时抛 :class:`DefinitionError`。"""
    errors = [i for i in validate(workflow) if i.level == "error"]
    if errors:
        raise DefinitionError(workflow.workflow_id, errors)
    return workflow


def _check_unique_ids(workflow: WorkflowDef) -> list[ValidationIssue]:
    seen: dict[str, int] = {}
    for node in workflow.nodes:
        seen[node.node_id] = seen.get(node.node_id, 0) + 1
    return [
        ValidationIssue("duplicate_node_id", f"节点 ID 重复出现 {count} 次", node_id)
        for node_id, count in seen.items()
        if count > 1
    ]


def _check_predecessors_exist(workflow: WorkflowDef) -> list[ValidationIssue]:
    known = {n.node_id for n in workflow.nodes}
    return [
        ValidationIssue(
            "unknown_predecessor",
            f"前置节点 {pred} 不在定义中",
            node.node_id,
        )
        for node in workflow.nodes
        for pred in node.predecessors
        if pred not in known
    ]


def _check_acyclic(workflow: WorkflowDef) -> list[ValidationIssue]:
    try:
        graphlib.TopologicalSorter(workflow.dependency_graph()).prepare()
    except graphlib.CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else []
        return [
            ValidationIssue(
                "cycle_detected",
                "存在环：" + " → ".join(str(c) for c in cycle),
            )
        ]
    return []


def _check_bindings(workflow: WorkflowDef) -> list[ValidationIssue]:
    """绑定的来源节点必须存在，且必须是该节点的祖先（不能向后引用）。

    「顺序依赖但无数据消费」的边在 DSL 中是合法且必须保留的，因此这里只校验
    **已被绑定引用**的来源，不要求存在对应边——反向要求（有边必须有绑定）不成立。
    """
    issues: list[ValidationIssue] = []
    known = workflow.node_map
    ancestors = _ancestors(workflow)

    for node in workflow.nodes:
        for binding in node.bindings:
            src_id, field = binding.source
            if src_id not in known:
                issues.append(
                    ValidationIssue(
                        "unknown_binding_source",
                        f"入参 {binding.target} 引用了不存在的节点 {src_id}",
                        node.node_id,
                    )
                )
                continue
            if src_id not in ancestors.get(node.node_id, set()):
                issues.append(
                    ValidationIssue(
                        "binding_source_not_ancestor",
                        f"入参 {binding.target} 引用了非祖先节点 {src_id}；"
                        "按原图报告不一致，不自动加边",
                        node.node_id,
                    )
                )
                continue
            outputs = known[src_id].outputs
            if outputs and field not in outputs:
                issues.append(
                    ValidationIssue(
                        "unknown_output_field",
                        f"入参 {binding.target} 引用了 {src_id} 未声明的输出字段 "
                        f"{field}（已声明：{list(outputs)}）",
                        node.node_id,
                        level="warning",
                    )
                )
    return issues


def _check_entry_reachable(workflow: WorkflowDef) -> list[ValidationIssue]:
    """所有节点都必须从入口可达。"""
    try:
        entry = workflow.resolve_entry()
    except (ValueError, KeyError) as exc:
        return [ValidationIssue("entry_unresolved", str(exc))]

    succ = workflow.successors()
    seen = {entry}
    stack = [entry]
    while stack:
        current = stack.pop()
        for nxt in succ.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)

    unreachable = [
        n.node_id for n in workflow.nodes if n.node_id not in seen
    ]
    issues: list[ValidationIssue] = []
    if unreachable:
        issues.append(
            ValidationIssue(
                "unreachable_nodes",
                f"以下节点从入口 {entry} 不可达：{unreachable}",
            )
        )
    return issues


def _check_terminals(workflow: WorkflowDef) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    starts = [n.node_id for n in workflow.nodes if n.node_type == "start"]
    ends = [n.node_id for n in workflow.nodes if n.node_type == "end"]
    if not starts:
        issues.append(ValidationIssue("no_start", "定义中缺少 start 节点"))
    if not ends:
        issues.append(
            ValidationIssue(
                "no_end",
                "定义中缺少 end 节点",
                level="warning",
            )
        )
    if workflow.entries:
        for e in workflow.entries:
            node = workflow.node_map.get(e)
            if node is None:
                issues.append(
                    ValidationIssue("entry_missing", f"声明的入口 {e} 不存在", e)
                )
            elif node.node_type != "start":
                issues.append(
                    ValidationIssue(
                        "entry_not_start",
                        f"声明的入口 {e} 类型为 {node.node_type}，非 start",
                        e,
                    )
                )
    return issues


def _ancestors(workflow: WorkflowDef) -> dict[str, set[str]]:
    """每个节点的全部祖先集合（不含自身）。"""
    preds = workflow.dependency_graph()
    order = workflow.dependency_order_safe()
    acc: dict[str, set[str]] = {n.node_id: set() for n in workflow.nodes}
    for node_id in order:
        for pred in preds.get(node_id, ()):
            acc[node_id].update(acc.get(pred, set()))
            acc[node_id].add(pred)
    return acc


def unreachable_effectors(workflow: WorkflowDef) -> list[NodeDef]:
    """返回「无下游」的节点，用于判断哪些节点决定最终输出。"""
    succ = workflow.successors()
    return [n for n in workflow.nodes if not succ.get(n.node_id)]

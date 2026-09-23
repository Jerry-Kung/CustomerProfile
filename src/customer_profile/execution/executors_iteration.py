"""``iteration`` 节点执行器。

Dify 的迭代由两半组成：``iteration`` 节点声明迭代配置，``iteration-start`` 是循环体
入口哨兵，循环体内的节点（``isInIteration`` 为真、带 ``iteration_id``）挂在**迭代节点
内部**，与父图没有直接连边。

三处关键口径，都是有意的：

1. **串行执行（P1，见差异清单）**：实测 8 处迭代全部是 ``is_parallel: false`` +
   ``parallel_nums: 10``。``is_parallel`` 是权威信号，``parallel_nums`` 在其下不生效，
   因此逐项串行，不引入并发。
2. **失败即终止（P2）**：8 处全部 ``error_handle_mode: terminated``。某项失败即终止
   整轮并抛出，由调度层按既有规则阻断下游；**已完成项的结果保留**，供人工核查。
3. **循环体节点不由父图调度**：它们在定义里由 ``config['iteration_id']`` 标记归属，
   父图遍历会跳过它们，改由本执行器逐项驱动。这样既满足「每次外部尝试都有记录」，
   又不会让循环体节点在父图里被错误地当作普通叶子跑一遍。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..definitions import NodeDef, NodeStatus
from .context import MissingValue, NodeResult, RunContext
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)

ITERATION_ID_KEY = "iteration_id"
"""节点配置里标记「归属哪个迭代」的键。"""


def register_all(registry: ExecutorRegistry) -> ExecutorRegistry:
    registry.register("iteration", execute_iteration)
    registry.register("iteration-start", execute_iteration_start)
    return registry


# ---------------------------------------------------------------- iteration-start


async def execute_iteration_start(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``iteration-start`` 哨兵：把当前项暴露为 ``item`` 与 ``index``。

    它自身不含逻辑，只是 Dify 表达「循环体入口」的方式（规划 §6.2：这类节点可在行为
    等价的前提下并入顺序代码）。在本实现里它仍是一个真实节点，因为循环体要按依赖顺序
    执行，哨兵提供了「谁的输出是当前项」这个明确的来源。
    """
    item = ctx.iteration_locals.get("item", _UNSET)
    if item is _UNSET:
        raise NodeExecutionError(
            f"iteration-start 节点 {node.node_id} 找不到当前迭代项；"
            "该节点只能在迭代循环体内执行"
        )
    return ExecutionOutcome(
        outputs={"item": item, "index": ctx.iteration_locals.get("index", 0)}
    )


# ---------------------------------------------------------------- iteration


async def execute_iteration(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """逐项执行循环体并收集输出。

    节点配置：

    - ``@iterator`` / ``iterator_selector`` 绑定：被遍历的数组来源；
    - ``@output`` / ``output_selector`` 绑定：每项收集的字段来源（在循环体内）；
    - ``body``：循环体节点 ID（不含迭代节点自身），由定义层按 DSL 的
      ``iteration_id`` 归属整理；
    - ``flatten_output``：是否把每项结果展开（实测全部为 ``true``）；
    - ``error_handle_mode``：``terminated`` 时某项失败即终止整轮。
    """
    body = tuple(node.config.get("body") or ())
    if not body:
        raise NodeExecutionError(
            f"iteration 节点 {node.node_id} 未声明 config['body']（循环体节点 ID）"
        )

    runner = getattr(rt, "iteration_node_runner", None)
    if runner is None:
        raise NodeExecutionError(
            f"iteration 节点 {node.node_id} 需要运行时提供循环体执行入口"
            "（NodeRuntime.iteration_node_runner）；装配服务时应由调度层注入"
        )

    items = _resolve_iterator(node, ctx)
    flatten = bool(node.config.get("flatten_output", True))
    error_mode = node.config.get("error_handle_mode", "terminated")
    output_field = node.config.get("output", "output")

    if not items:
        # 对空数组：产出空结果。各截图流程的上游分支已保证「无文件」时不进入迭代，
        # 这里是兜底，不把「没有可迭代项」伪装成错误。
        return ExecutionOutcome(
            outputs={output_field: []},
            meta={"iterations": 0, "flatten_output": flatten},
        )

    collected: list[Any] = []
    for index, item in enumerate(items):
        try:
            values = await _run_one_item(node, ctx, rt, runner, item, index, body)
        except Exception as exc:
            if error_mode != "terminated":
                collected.append(None)
                continue
            raise NodeExecutionError(
                f"iteration 节点 {node.node_id} 第 {index} 项失败，"
                f"按 error_handle_mode=terminated 终止整轮（已完成 {index} 项，"
                f"其结果保留）：{type(exc).__name__}: {exc}"
            ) from exc
        if flatten:
            collected.extend(values)
        else:
            collected.append(values[0] if len(values) == 1 else values)

    return ExecutionOutcome(
        outputs={output_field: collected},
        meta={"iterations": len(items), "flatten_output": flatten},
    )


async def _run_one_item(
    node: NodeDef,
    ctx: RunContext,
    rt: NodeRuntime,
    runner: Any,
    item: Any,
    index: int,
    body: Sequence[str],
) -> list[Any]:
    """执行一轮循环体，返回该项按 ``@output`` 收集到的值列表。

    每项用一个**子上下文**：它继承父上下文已完成的输出（循环体常引用迭代外的节点），
    并把当前项以 ``item`` / ``index`` 注入。子上下文把结果写回父上下文，因此迭代
    节点自身仍能按图语义被下游引用。
    """
    item_ctx = ctx.fork_iteration(
        item=item, index=index, iteration_id=node.node_id
    )
    for node_id in body:
        body_node = ctx.workflow_node(node_id)
        execution = await runner(
            body_node, item_ctx, iteration_id=node.node_id, index=index
        )
        if execution.status != NodeStatus.SUCCEEDED:
            raise NodeExecutionError(
                f"循环体节点 {node_id}（{body_node.title}）第 {index} 项失败："
                f"{execution.error}"
            )
        item_ctx.record(
            NodeResult(
                node_id=node_id,
                outputs=dict(execution.outputs),
                status=execution.status,
            )
        )
    return _extract_outputs(node, item_ctx, index)


def _extract_outputs(node: NodeDef, item_ctx: RunContext, index: int) -> list[Any]:
    sources = _output_sources(node)
    if not sources:
        raise NodeExecutionError(
            f"iteration 节点 {node.node_id} 未声明 @output（每项收集的字段来源）"
        )
    values: list[Any] = []
    for source in sources:
        try:
            values.append(item_ctx.resolve(source))
        except (MissingValue, KeyError) as exc:
            raise NodeExecutionError(
                f"iteration 节点 {node.node_id} 第 {index} 项的 @output 引用 "
                f"{source[0]}.{source[1]} 未产出：{exc}"
            ) from exc
    return values


def _resolve_iterator(node: NodeDef, ctx: RunContext) -> list[Any]:
    """取被遍历的数组。上游 code 节点常产出 JSON 字符串，这里按 JSON 解析。"""
    for binding in node.bindings:
        if binding.target in {"@iterator", "iterator_selector"}:
            return _as_list(resolve_binding_value(ctx, binding))
    declared = node.config.get("iterator")
    if declared:
        return _as_list(ctx.resolve_optional(tuple(declared), default=None))
    raise NodeExecutionError(
        f"iteration 节点 {node.node_id} 未声明 @iterator（被遍历的数组来源）"
    )


def _output_sources(node: NodeDef) -> list[tuple[str, str]]:
    return [
        binding.source
        for binding in node.bindings
        if binding.target in {"@output", "output_selector"}
    ]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        import json

        try:
            parsed = json.loads(text)
        except ValueError:
            return [value]
        return list(parsed) if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<unset>"


_UNSET = _Unset()

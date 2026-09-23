"""``variable-aggregator`` 节点执行器。

DSL 里这个节点负责「多个来源里取一个可用值」，两种形态：

- **普通形态**（22 处）：给一组 ``variables``，取其中第一个可用的。
- **分组形态**（1 处，``人设特征判断`` 节点 ``1776760747042``）：``advanced_settings``
  之下开 ``group_enabled``，每组各自聚合，输出是一个**多字段对象**，下游按属性取。

**取值口径（有意差异 P4，见差异清单）**：判据是「该来源节点是否真的执行过」
（``ctx.has``），而不是「取到的值是否非空」。理由：空字符串是合法产出——
``无数据，输出默认信息`` 这类模板节点的正常结果就是一段固定提示文本、各截图流程的
code 节点在「没找到该 channel」时也**明确返回空串**。若用非空判据，这些正常结果会被
误判成不可用，聚合器转而取另一个分支的值，从而静默改变业务结果。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..definitions import NodeDef
from .context import MissingValue, RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)


def register_all(registry: ExecutorRegistry) -> ExecutorRegistry:
    registry.register("variable-aggregator", execute_variable_aggregator)
    return registry


async def execute_variable_aggregator(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """按声明顺序取第一个「已执行」来源的值。

    节点配置：

    - ``variables``：``[(node_id, field), ...]``，按优先级排列。也可由绑定的
      ``@variables`` 键给出（定义层通常直接写在 ``config["variables"]``）；
    - ``groups``：分组形态，``[{name, output_type, variables}, ...]``；
    - ``output``：输出字段名（缺省 ``output``，与 Dify 的默认输出名一致）；
    - ``output_type``：用于回写时的类型还原（``string`` / ``any`` …）。
    """
    groups = node.config.get("groups")
    if groups:
        return _execute_grouped(node, ctx, groups)

    sources = _declared_sources(node, ctx)
    if not sources:
        raise NodeExecutionError(
            f"variable-aggregator 节点 {node.node_id} 未声明可用的来源变量"
        )

    output_field = node.config.get("output", "output")
    for source in sources:
        value = ctx.resolve_optional(source, default=_UNSET)
        if value is not _UNSET:
            return ExecutionOutcome(
                outputs={output_field: value},
                meta={"aggregated_from": f"{source[0]}.{source[1]}"},
            )

    # 一个来源都没执行过：不是「取到空值」而是「无值可取」。如实报错，
    # 不用空串伪装成成功——那会让下游拿着一个来源不明的空值继续跑。
    raise NodeExecutionError(
        f"variable-aggregator 节点 {node.node_id} 的全部来源都未产出结果："
        f"{[f'{n}.{f}' for n, f in sources]}"
    )


def _execute_grouped(
    node: NodeDef, ctx: RunContext, groups: Any
) -> ExecutionOutcome:
    """分组聚合：每组各取第一个已执行的来源，每组包成 ``{"output": 值}``。

    输出字段名即组名（``hobby`` / ``consumption``）。**每组的值是对象而不是裸值**：
    DSL 里下游 ``code`` 节点取的是 ``hobby_result_object["output"]``，且把这两个入参
    声明为 ``value_type: object``。返回裸字符串会让那段 code 直接 ``KeyError``，
    因此这里按下游实际消费的形态产出（P3 的实测口径）。
    """
    result: dict[str, Any] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            raise NodeExecutionError(
                f"variable-aggregator 节点 {node.node_id} 的分组声明格式非法：{group!r}"
            )
        name = group.get("group_name") or group.get("name")
        if not name:
            raise NodeExecutionError(
                f"variable-aggregator 节点 {node.node_id} 的分组缺少 group_name"
            )

        variables = _normalise_variables(group.get("variables"))
        if not variables:
            raise NodeExecutionError(
                f"variable-aggregator 节点 {node.node_id} 的分组 {name!r} 没有来源变量"
            )

        value: Any = _UNSET
        origin: str | None = None
        for source in variables:
            value = ctx.resolve_optional(source, default=_UNSET)
            if value is not _UNSET:
                origin = f"{source[0]}.{source[1]}"
                break

        if value is _UNSET:
            raise NodeExecutionError(
                f"variable-aggregator 节点 {node.node_id} 的分组 {name!r} 全部来源都未产出结果："
                f"{[f'{n}.{f}' for n, f in variables]}"
            )
        # 包成 {"output": 值}：下游 code 按 ["output"] 取值（DSL 已明确）
        result[name] = {"output": value}
        if origin:
            result.setdefault("_origins", {})[name] = origin

    return ExecutionOutcome(
        outputs=dict(result),
        meta={"groups": sorted(k for k in result if not k.startswith("_"))},
    )


class _Unset:
    """区分「取到 None」与「来源未执行」的哨兵。

    ``ctx.resolve_optional`` 缺省返回 ``None``，而 ``None`` 是合法值（模板渲染时
    归一为空串）。用独立哨兵才能把两者分开。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<unset>"


_UNSET = _Unset()


def _declared_sources(node: NodeDef, ctx: RunContext) -> list[tuple[str, str]]:
    """收集聚合来源。优先取 ``config['variables']``，否则取 ``@variables`` 绑定。"""
    declared = node.config.get("variables")
    if declared:
        return _normalise_variables(declared)

    for binding in node.bindings:
        if binding.target in {"@variables", "variables"}:
            return [(binding.source[0], binding.source[1])]
    return []


def _normalise_variables(raw: Any) -> list[tuple[str, str]]:
    """把 ``[[node, field], ...]`` 或 ``[(node, field)]`` 归一到二元组列表。"""
    if not raw:
        return []
    sources: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            node_id, _, field = item.partition(".")
            sources.append((node_id, field))
            continue
        parts = tuple(item)
        if len(parts) != 2:
            raise NodeExecutionError(f"聚合来源应为 (node_id, field)，实际为 {item!r}")
        sources.append((parts[0], parts[1]))
    return sources

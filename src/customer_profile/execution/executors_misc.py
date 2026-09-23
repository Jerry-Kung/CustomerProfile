"""非外部依赖的节点执行器：start / end / code / template-transform / if-else。

``code`` 节点遵循规划 §6.2：原节点代码逻辑**直接提取**为普通 Python 函数，
只剥离 Dify 特有的入参加载与返回值包装，不在迁移中顺手重构。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Mapping, Sequence

from ..definitions import NodeDef
from ..templates import TemplateRepository
from .context import MissingValue, RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)


def register_all(
    registry: ExecutorRegistry, *, template_repository: TemplateRepository | None = None
) -> ExecutorRegistry:
    """注册本模块的执行器。"""
    registry.register("start", execute_start)
    registry.register("end", execute_end)
    registry.register("code", execute_code)
    registry.register("template-transform", execute_template_transform)
    registry.register("if-else", execute_if_else)
    if template_repository is not None:
        registry.register("template", make_template_executor(template_repository))
    return registry


# ---------------------------------------------------------------- start / end


async def execute_start(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``start`` 节点：把工作流入参原样暴露为输出。

    Dify 的 ``start`` 声明的变量即工作流入参，因此在迁移后它就是函数签名参数
    的具体化：定义里只需要字段名，值由调用方传入。
    """
    declared = node.config.get("variables") or tuple(ctx.inputs)
    outputs: dict[str, Any] = {}
    missing: list[str] = []
    for name in declared:
        if name in ctx.inputs:
            outputs[name] = ctx.inputs[name]
        elif _is_required_input(node, name):
            missing.append(name)
    if missing:
        raise NodeExecutionError(
            f"工作流入参缺失：{missing}；已提供：{sorted(ctx.inputs)}"
        )
    # 调用方多传的入参一并透出，避免定义与调用方轻微不一致时静默丢参
    for name, value in ctx.inputs.items():
        outputs.setdefault(name, value)
    return ExecutionOutcome(outputs=outputs)


def _is_required_input(node: NodeDef, name: str) -> bool:
    """某个工作流入参是否必填。

    定义层用 ``required_<name>`` 传递（``required_phone_number=True``），这是一直以来的
    写法；而这里原先读的是 ``required.<name>``，两种拼法对不上，于是**所有**入参都落到
    默认的「必填」，``required_data_source=False`` 这类声明形同虚设——调用方少传一个
    可选参数就会在 ``start`` 节点直接失败。两种拼法都认，缺省仍为必填。
    """
    for key in (f"required_{name}", f"required.{name}"):
        if key in node.config:
            return bool(node.config[key])
    return True


async def execute_end(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``end`` 节点：按定义的输出字段收集结果。

    ``result1`` 为字符串是现有对外契约（`Dify迁移任务说明.md` §3.1）；要改为对象
    必须作为单独的 API 版本变更，因此这里对 ``result1`` 强制字符串化。
    """
    outputs: dict[str, Any] = {}
    for binding in node.bindings:
        value = resolve_binding_value(ctx, binding)
        if binding.target == "result1" and not isinstance(value, str):
            value = coerce_to_text(value)
        outputs[binding.target] = value
    # 定义里没有绑定的声明字段，按同名规则从上游节点找
    for field_name in node.outputs:
        if field_name in outputs:
            continue
        found = _find_upstream_field(ctx, node, field_name)
        if found is not None:
            outputs[field_name] = found
    return ExecutionOutcome(outputs=outputs)


def _find_upstream_field(ctx: RunContext, node: NodeDef, field_name: str) -> Any:
    for pred in node.predecessors:
        if not ctx.has(pred):
            continue
        result = ctx.result_of(pred)
        if field_name in result.outputs:
            value = result.outputs[field_name]
            return coerce_to_text(value) if field_name == "result1" else value
    return None


# ---------------------------------------------------------------- code


async def execute_code(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``code`` 节点：调用已登记的普通 Python 函数。

    约定：函数以 ``**inputs`` 接收已解析的入参，返回字段字典。函数本身不含任何
    Dify 概念——入参加载与返回值包装都在这里完成。
    """
    function = _lookup_code_function(node, rt)
    inputs = ctx.bind_inputs(node.bindings)

    if inspect.iscoroutinefunction(function):
        raw = await function(**inputs)
    else:
        raw = await asyncio.to_thread(function, **inputs)

    if raw is None:
        raise NodeExecutionError(
            f"code 节点 {node.node_id} 的 {getattr(function, '__name__', function)!r} "
            "返回 None；应返回字段字典"
        )
    if not isinstance(raw, Mapping):
        raise NodeExecutionError(
            f"code 节点 {node.node_id} 返回值应为字段字典，实际为 {type(raw)!r}"
        )
    return ExecutionOutcome(outputs=dict(raw))


def _lookup_code_function(node: NodeDef, rt: NodeRuntime) -> Callable[..., Any]:
    from .code_registry import get_code_function

    ref = node.config.get("function")
    if not ref:
        raise NodeExecutionError(
            f"code 节点 {node.node_id} 未声明 config['function']；"
            "code 节点必须指向一个已登记的普通 Python 函数"
        )
    try:
        return get_code_function(ref, extra=rt.extra.get("code_functions"))
    except KeyError as exc:
        raise NodeExecutionError(str(exc)) from exc


# ---------------------------------------------------------------- template-transform


async def execute_template_transform(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``template-transform`` 节点：按模板资源渲染出文本。

    迁移后一个模板节点等价于一次渲染函数调用；同一模块的多个模板节点可合并为顺序
    代码，提示词正文仍独立存放（§6.5）。
    """
    repository = _template_repository(rt)
    template_name = node.config.get("template")
    variables: dict[str, Any] = {}
    for binding in node.bindings:
        variables[binding.target] = resolve_binding_value(ctx, binding)

    inline = node.config.get("template_text")
    strict = node.config.get("strict", True)

    if template_name:
        text = repository.render(template_name, variables, strict=strict)
    elif inline is not None:
        from ..templates import render_text

        text = render_text(inline, variables, strict=strict)
    else:
        raise NodeExecutionError(
            f"template-transform 节点 {node.node_id} 既未声明 config['template']，"
            "也未声明 config['template_text']"
        )

    output_field = node.config.get("output", "output")
    return ExecutionOutcome(outputs={output_field: text})


def make_template_executor(
    repository: TemplateRepository,
) -> Callable[..., Any]:
    """构造一个持有模板仓库的 template-transform 执行器（供独立测试使用）。"""

    async def executor(
        node: NodeDef, ctx: RunContext, rt: NodeRuntime
    ) -> ExecutionOutcome:
        rt.extra.setdefault("template_repository", repository)
        return await execute_template_transform(node, ctx, rt)

    return executor


def _template_repository(rt: NodeRuntime) -> TemplateRepository:
    repository = rt.extra.get("template_repository")
    if repository is None:
        repository = TemplateRepository()
        rt.extra["template_repository"] = repository
    return repository


# ---------------------------------------------------------------- if-else


async def execute_if_else(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """``if-else`` 节点：按声明顺序求值条件，命中即返回该分支。

    分支语义在迁移后退化为普通 ``if / elif / else``（§6.2）。条件表达式写成
    ``{"field": 入参名, "operator": "...", "value": ...}`` 的声明式结构，
    避免在定义里塞入可执行字符串。
    """
    branches = node.config.get("branches")
    if not branches:
        raise NodeExecutionError(
            f"if-else 节点 {node.node_id} 未声明 config['branches']"
        )

    inputs = ctx.bind_inputs(node.bindings)
    for branch in branches:
        condition = branch.get("condition") or {}
        if evaluate_condition(condition, inputs):
            branch_id = branch.get("id") or branch.get("name") or "true"
            output = branch.get("output")
            outputs = {output: inputs.get(output)} if output else {}
            return ExecutionOutcome(outputs=outputs, branch=branch_id)

    fallback = node.config.get("else")
    fallback_id = node.config.get("else_id", "else")
    outputs = {}
    if fallback:
        outputs = {fallback: inputs.get(fallback)}
    return ExecutionOutcome(outputs=outputs, branch=fallback_id)


def evaluate_condition(condition: Mapping[str, Any], inputs: Mapping[str, Any]) -> bool:
    """求值一条条件声明。

    支持的 ``operator``：``eq`` / ``ne`` / ``empty`` / ``not_empty`` / ``contains`` /
    ``not_contains`` / ``starts_with`` / ``ends_with`` / ``gt`` / ``lt`` / ``in`` /
    ``is_true`` / ``is_false``。未知操作符直接报错，不做「默认放行」。
    """
    field_name = condition.get("field")
    if not field_name:
        raise NodeExecutionError(f"条件缺少 field：{condition!r}")
    if field_name not in inputs:
        raise NodeExecutionError(
            f"条件引用了未绑定的入参 {field_name!r}；已绑定：{sorted(inputs)}"
        )

    actual = inputs[field_name]
    operator = condition.get("operator", "not_empty")
    expected = condition.get("value")

    if operator == "eq":
        return _text(actual) == _text(expected)
    if operator == "ne":
        return _text(actual) != _text(expected)
    if operator == "empty":
        return _is_empty(actual)
    if operator == "not_empty":
        return not _is_empty(actual)
    if operator == "contains":
        return _text(expected) in _text(actual)
    if operator == "not_contains":
        return _text(expected) not in _text(actual)
    if operator == "starts_with":
        return _text(actual).startswith(_text(expected))
    if operator == "ends_with":
        return _text(actual).endswith(_text(expected))
    if operator == "in":
        return _text(actual) in [ _text(v) for v in _as_sequence(expected) ]
    if operator == "gt":
        return _as_number(actual) > _as_number(expected)
    if operator == "lt":
        return _as_number(actual) < _as_number(expected)
    if operator == "is_true":
        return actual is True or _text(actual).lower() in {"true", "1", "yes"}
    if operator == "is_false":
        return actual is False or _text(actual).lower() in {"false", "0", "no"}

    raise NodeExecutionError(f"不支持的条件操作符 {operator!r}")


def _text(value: Any) -> str:
    return coerce_to_text(value).strip()


def _is_empty(value: Any) -> bool:
    from .context import is_empty

    return is_empty(value)


def _as_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_number(value: Any) -> float:
    try:
        return float(_text(value))
    except ValueError as exc:
        raise NodeExecutionError(f"条件比较需要数值，实际为 {value!r}") from exc

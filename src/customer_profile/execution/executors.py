"""节点执行器注册表与内置执行器。

对齐 `docs/specs/V0迁移规划.md` §6.2 的「节点 → Python」映射原则：执行器只负责把
节点声明翻译成一次函数调用，业务逻辑本身写在 `workflows/` 下的普通 Python 里。

V0.3 覆盖 DSL 的全部节点类型：start / end / code / template-transform / if-else /
llm（含 vision）/ http-request / tool / iteration（含 iteration-start）/
variable-aggregator（含分组）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..definitions import Binding, NodeDef
from .context import MissingValue, NodeResult, RunContext, coerce_to_text

# ---------------------------------------------------------------- 结果与异常


@dataclass(slots=True)
class ExecutionOutcome:
    """执行器返回的原始结果。``branch`` 供 if-else 声明走哪一支。"""

    outputs: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class NodeExecutionError(Exception):
    """节点执行失败。``retryable`` 供调度层判断是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class NodeRuntime:
    """执行器在运行时需要的一切依赖，避免它们各自去 import 全局状态。"""

    settings: Any
    """:class:`customer_profile.settings.Settings`。"""

    request_limiter: Any
    """外部请求限额（同时约束 LLM 与 HTTP 的真实出网调用）。"""

    llm: Any
    """:class:`customer_profile.execution.llm.LlmClient`。"""

    http: Any
    """:class:`customer_profile.execution.http.HttpClient`。"""

    recorder: Any = None
    """留痕入口，可为 ``None``（单测中不需要落库）。"""

    subworkflow_runner: Callable[..., Awaitable[dict[str, Any]]] | None = None
    """``tool`` 节点调用子工作流时使用；由调度层注入，避免循环导入。"""

    iteration_node_runner: Callable[..., Awaitable[Any]] | None = None
    """``iteration`` 节点逐项驱动循环体节点时使用；同样由调度层注入。

    迭代循环体节点不在父图的调度集合里，因此它需要这个入口去跑一个「图外节点」。
    与 ``subworkflow_runner`` 一样，走注入而不是让执行器去 import 调度器。
    """

    extra: dict[str, Any] = field(default_factory=dict)
    """节点实现可能需要的其他服务（如模板仓库）。"""


class NodeExecutor(Protocol):
    """节点执行函数签名。返回 :class:`ExecutionOutcome` 或一个字段字典。"""

    async def __call__(
        self, node: NodeDef, ctx: RunContext, rt: NodeRuntime
    ) -> ExecutionOutcome | Mapping[str, Any]: ...


# ---------------------------------------------------------------- 注册表


class ExecutorRegistry:
    """按节点类型分派的执行器表。"""

    def __init__(self) -> None:
        self._table: dict[str, NodeExecutor] = {}

    def register(
        self, node_type: str, executor: NodeExecutor | None = None
    ) -> Any:
        """注册执行器。可作装饰器使用：``@registry.register("code")``。"""
        if executor is None:

            def decorator(func: NodeExecutor) -> NodeExecutor:
                self._table[node_type] = func
                return func

            return decorator
        self._table[node_type] = executor
        return executor

    def get(self, node_type: str) -> NodeExecutor:
        try:
            return self._table[node_type]
        except KeyError as exc:
            raise NodeExecutionError(
                f"节点类型 {node_type!r} 尚无执行器；V0.2 仅覆盖 "
                f"{sorted(self._table)}"
            ) from exc

    def supports(self, node_type: str) -> bool:
        return node_type in self._table

    @property
    def node_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))


def build_default_registry(
    template_repository: Any = None,
    code_functions: Mapping[str, Callable[..., Any]] | None = None,
) -> ExecutorRegistry:
    """装配 V0.2 的全部内置执行器。"""
    from . import (
        executors_aggregator,
        executors_http,
        executors_iteration,
        executors_llm,
        executors_misc,
    )

    registry = ExecutorRegistry()
    executors_misc.register_all(registry, template_repository=template_repository)
    executors_llm.register_all(registry)
    executors_http.register_all(registry)
    executors_iteration.register_all(registry)
    executors_aggregator.register_all(registry)
    return registry


def normalise_outcome(
    raw: ExecutionOutcome | Mapping[str, Any] | None,
) -> ExecutionOutcome:
    """把执行器的宽松返回值归一成 :class:`ExecutionOutcome`。"""
    if raw is None:
        return ExecutionOutcome()
    if isinstance(raw, ExecutionOutcome):
        return raw
    if isinstance(raw, Mapping):
        return ExecutionOutcome(outputs=dict(raw))
    raise TypeError(f"执行器返回类型不受支持：{type(raw)!r}")


def resolve_binding_value(ctx: RunContext, binding: Binding) -> Any:
    """解析单条绑定，错误信息带上节点上下文。"""
    try:
        return ctx.resolve(binding.source)
    except MissingValue as exc:
        raise NodeExecutionError(
            f"入参 {binding.target} 引用了 {binding.source[0]}.{binding.source[1]}，"
            f"但该值不可用：{exc}"
        ) from exc

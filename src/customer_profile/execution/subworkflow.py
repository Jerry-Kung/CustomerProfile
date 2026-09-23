"""子工作流调用：把 ``tool`` 节点的子流程调用变成一次嵌套运行。

对齐 `Dify迁移任务说明.md` §5.3：

- 子工作流是**被多个节点复用的 Python 定义**，不是「按名字替换成单次模型请求」；
- 用 ``parent_run_id`` + ``parent_node_id`` + ``call_path`` 区分同一静态节点 ID 的
  多次动态执行；
- 父节点在等待期间不占用子流程所需的**外部请求名额**，避免父子互等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..definitions import NodeDef, WorkflowDef
from .context import RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)


@dataclass(slots=True)
class CallPath:
    """动态调用路径。

    形如 ``/0/2/1``：从根运行出发，每一段是该层父节点在**该层运行内的执行序号**。
    同一静态节点 ID 被复用多次时，静态 ID 不足以唯一定位，``call_path`` 可以。
    """

    segments: tuple[int, ...] = ()

    def child(self, index: int) -> "CallPath":
        return CallPath(self.segments + (index,))

    def __str__(self) -> str:
        return "/" + "/".join(str(s) for s in self.segments) if self.segments else "/"

    @classmethod
    def parse(cls, value: str | None) -> "CallPath":
        if not value or value == "/":
            return cls()
        return cls(tuple(int(p) for p in value.strip("/").split("/") if p != ""))


class CallPathAllocator:
    """按父节点分配子调用序号。一次运行一个实例，保证序号稳定可复现。"""

    def __init__(self, base: CallPath | None = None) -> None:
        self._base = base or CallPath()
        self._counters: dict[str, int] = {}

    def next_for(self, parent_node_id: str) -> CallPath:
        index = self._counters.get(parent_node_id, 0)
        self._counters[parent_node_id] = index + 1
        return self._base.child(index)


class SubWorkflowRunner:
    """执行 ``tool`` 节点引用的子工作流。"""

    def __init__(
        self,
        registry: ExecutorRegistry,
        runtime: NodeRuntime,
        *,
        definitions: Mapping[str, WorkflowDef] | None = None,
        scheduler_factory: Any = None,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.definitions = dict(definitions or {})
        self._scheduler_factory = scheduler_factory

    def register(self, workflow: WorkflowDef) -> None:
        self.definitions[workflow.workflow_id] = workflow

    def resolve(self, key: str) -> WorkflowDef:
        try:
            return self.definitions[key]
        except KeyError as exc:
            raise NodeExecutionError(
                f"子工作流 {key!r} 未注册；已注册：{sorted(self.definitions)}"
            ) from exc

    async def __call__(
        self,
        *,
        workflow_id: str,
        inputs: Mapping[str, Any],
        ctx: RunContext,
        node: NodeDef,
        call_path: CallPath,
    ) -> ExecutionOutcome:
        """运行一个子工作流，返回它的 ``end`` 节点输出。

        子运行以 ``is_child=True`` 交给调度器，因此不申请执行名额；
        它内部真实的 LLM / HTTP 调用仍逐次计入统一限额。
        """
        from .scheduler import RunRequest, Scheduler  # 避免循环导入

        workflow = self.resolve(workflow_id)
        if self._scheduler_factory is not None:
            scheduler = self._scheduler_factory()
        else:
            # 子运行必须沿用父运行的留痕器，否则子运行与其节点一次都不会落库，
            # V0.4 的「下钻到 17 处 Gemini 调用的每一次尝试」就无从谈起。
            scheduler = Scheduler(
                self.registry,
                self.runtime,
                recorder=getattr(self.runtime, "recorder", None),
            )

        request = RunRequest(
            workflow=workflow,
            inputs=dict(inputs),
            call_path=str(call_path),
            parent_run_id=ctx.run_id,
            parent_node_id=node.node_id,
            business_ref=ctx.inputs.get("phone_number"),
            is_child=True,
        )
        outcome = await scheduler.run(request)

        if not outcome.succeeded:
            raise NodeExecutionError(
                f"子工作流 {workflow_id} 失败（run={outcome.run_id}）：{outcome.error}"
            )

        # 子流程的输出字段经父节点透出；同时保留 sub_run_id 供下钻（V0.4）
        return ExecutionOutcome(
            outputs=dict(outcome.outputs),
            meta={"sub_run_id": outcome.run_id, "child_workflow_id": workflow_id},
        )


LITERAL_PREFIX = "@literal:"
"""``@inputs`` 中标记「这是常量而非父节点入参名」的前缀。"""


def make_tool_executor(runner: SubWorkflowRunner) -> Any:
    """构造 ``tool`` 节点的执行器。

    节点配置：``@workflow``（子工作流 ID）、``@inputs``（子流程入参名 → 来源）。

    ``@inputs`` 的值有两种形态：

    - 父节点入参名（如 ``"phone_number"``）：从父节点绑定实参里取同名入参；
    - 字面量（如 ``"@literal:WeChatMomentsScreenshot"``）：直接作为常量传给子流程。

    字面量形态是 V0.3 新增的：DSL 里子流程入参存在固定值，典型是截图类工作流传的
    ``data_source``（每个子流程对应一种数据渠道）。这类参数在父图里没有对应入参，
    按名字映射会找不到而报错。
    """

    async def execute_tool(
        node: NodeDef, ctx: RunContext, rt: NodeRuntime
    ) -> ExecutionOutcome:
        workflow_id = node.config.get("@workflow")
        if not workflow_id:
            raise NodeExecutionError(
                f"tool 节点 {node.node_id} 未声明 config['@workflow']；"
                "子流程必须按 ID 显式引用，不能按名称猜"
            )

        mapping = node.config.get("@inputs") or {}
        bound = ctx.bind_inputs(node.bindings)

        inputs: dict[str, Any] = {}
        for child_param, source in mapping.items():
            if isinstance(source, str) and source.startswith(LITERAL_PREFIX):
                inputs[child_param] = source[len(LITERAL_PREFIX) :]
                continue
            if source in bound:
                inputs[child_param] = bound[source]
            else:
                raise NodeExecutionError(
                    f"tool 节点 {node.node_id} 声明子流程入参 {child_param} 取自 "
                    f"{source!r}，但该父节点入参未绑定；已绑定：{sorted(bound)}"
                )
        # 未在映射中声明的绑定，按同名透传
        for name, value in bound.items():
            inputs.setdefault(name, value)

        call_path = _next_call_path(ctx, rt, node)
        return await runner(
            workflow_id=workflow_id,
            inputs=inputs,
            ctx=ctx,
            node=node,
            call_path=call_path,
        )

    return execute_tool


def _next_call_path(ctx: RunContext, rt: NodeRuntime, node: NodeDef) -> CallPath:
    """取本次子调用的动态路径。

    分配器按 ``run_id`` 挂在 ``rt.extra`` 上。之所以不放 ``RunContext``：它用了
    ``slots=True``（每次运行创建一份，省内存），不能任意加属性；放运行时也更贴合
    「一次运行的调度状态」这个归属。

    序号按**同一父节点**累计，因此同一 ``tool`` 节点被复用多次时，各次调用得到
    ``/0``、``/1``…… 互不混淆。
    """
    allocators = rt.extra.setdefault("call_path_allocators", {})
    allocator = allocators.get(ctx.run_id)
    if allocator is None:
        allocator = CallPathAllocator(CallPath.parse(ctx.call_path))
        allocators[ctx.run_id] = allocator
    return allocator.next_for(node.node_id)


def register_tool_executor(
    registry: ExecutorRegistry,
    runtime: NodeRuntime,
    definitions: Mapping[str, WorkflowDef],
) -> SubWorkflowRunner:
    """把 ``tool`` 执行器装进注册表，返回可复用的 runner。"""
    runner = SubWorkflowRunner(registry, runtime, definitions=definitions)
    runtime.subworkflow_runner = runner
    registry.register("tool", make_tool_executor(runner))
    return runner

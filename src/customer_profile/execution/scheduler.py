"""调度层：按依赖就绪启动，不逐层等待。

对齐的硬约束（`Dify迁移任务说明.md` §5.2、`docs/specs/V0迁移规划.md` §5）：

1. **依赖就绪即启动**，不做全图逐层屏障；
2. **多前置汇合节点只执行一次**；任一前置未完成则不得读取其输出；
3. **父子不互等**——父节点等待子流程期间不占用子流程所需的**外部请求名额**，
   否则并发上限为 1 时必然死锁。实现方式是两级独立信号量：
   ``active_slot`` 限制同时执行的节点数，``request_limiter`` 限制真实出网请求数；
   子运行不再申请 ``active_slot``。
4. 每个 run 独立上下文，禁止跨客户共享可变中间结果。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..definitions import NodeDef, NodeStatus, RunStatus, WorkflowDef
from ..validation import require_valid
from .context import MissingValue, NodeResult, RunContext
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    normalise_outcome,
)


@dataclass(slots=True)
class NodeExecution:
    """一次节点执行的完整结果。"""

    node_id: str
    title: str
    node_type: str
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    queued_at_ms: int = 0
    started_at_ms: int = 0
    duration_ms: int = 0
    branch: str | None = None
    sub_run_id: str | None = None
    """子流程节点触发的子运行 ID，供下钻（V0.4）。"""

    @property
    def succeeded(self) -> bool:
        return self.status == NodeStatus.SUCCEEDED


@dataclass(slots=True)
class RunOutcome:
    """一次工作流运行的结果。"""

    run_id: str
    workflow_id: str
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    node_executions: list[NodeExecution] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    sub_run_ids: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == RunStatus.SUCCEEDED

    def node(self, node_id: str) -> NodeExecution | None:
        for execution in self.node_executions:
            if execution.node_id == node_id:
                return execution
        return None


class RunRecorder:
    """留痕接口。全部方法可为空实现——单测不需要数据库。"""

    async def run_started(self, run: Any) -> None: ...

    async def node_started(self, run: Any, node: NodeDef) -> None: ...

    async def node_finished(self, run: Any, execution: NodeExecution) -> None: ...

    async def attempt_recorded(
        self, node_ref: Any, attempt: Any, payload: Mapping[str, Any] | None = None
    ) -> None: ...

    async def run_finished(self, run: Any, outcome: RunOutcome) -> None: ...


@dataclass(slots=True)
class RunRequest:
    """一次运行的请求描述。"""

    workflow: WorkflowDef
    inputs: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    call_path: str = ""
    parent_run_id: str | None = None
    parent_node_id: str | None = None
    business_ref: str | None = None
    """业务请求标识（如客户手机号），用于列表检索。"""

    is_child: bool = False
    """子运行不再申请 ``active_slot``，避免父子互等。"""


class Scheduler:
    """单进程内的工作流调度器。"""

    def __init__(
        self,
        registry: ExecutorRegistry,
        runtime: NodeRuntime,
        *,
        max_active_nodes: int = 8,
        recorder: RunRecorder | None = None,
        run_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.recorder = recorder or RunRecorder()
        self._clock = clock
        self._run_id_factory = run_id_factory or _default_run_id
        self._active_slot = asyncio.Semaphore(max_active_nodes)
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._contexts: dict[str, RunContext] = {}

    def context_of(self, run_id: str) -> RunContext | None:
        """按 run_id 取运行上下文。留痕层在快照入参时需要它。"""
        return self._contexts.get(run_id)

    # ------------------------------------------------------------ 公共入口

    async def run(self, request: RunRequest) -> RunOutcome:
        """执行一个工作流并返回结果。不抛业务异常——失败写进 ``RunOutcome``。"""
        workflow = request.workflow
        require_valid(workflow)
        workflow = workflow.normalise()

        run_id = request.run_id or self._run_id_factory()
        ctx = RunContext(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            inputs=dict(request.inputs),
            call_path=request.call_path,
            parent_run_id=request.parent_run_id,
            parent_node_id=request.parent_node_id,
        )
        record = _RunRecord(request=request, workflow=workflow, run_id=run_id)
        self._contexts[run_id] = ctx
        await self.recorder.run_started(record)

        started = self._clock()
        executions: list[NodeExecution] = []
        try:
            executions = await self._execute_graph(
                workflow, ctx, record, is_child=request.is_child
            )
        except asyncio.CancelledError:
            outcome = RunOutcome(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                status=RunStatus.CANCELLED,
                node_executions=executions,
                error="运行被取消",
                duration_ms=int((self._clock() - started) * 1000),
            )
            await self.recorder.run_finished(record, outcome)
            self._contexts.pop(run_id, None)
            raise

        status, outputs, error = self._summarise(workflow, ctx, executions)
        outcome = RunOutcome(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            status=status,
            outputs=outputs,
            node_executions=executions,
            error=error,
            duration_ms=int((self._clock() - started) * 1000),
            sub_run_ids=[e.sub_run_id for e in executions if e.sub_run_id],
        )
        await self.recorder.run_finished(record, outcome)
        self._contexts.pop(run_id, None)
        return outcome

    async def submit(self, request: RunRequest) -> asyncio.Task[RunOutcome]:
        """把运行放进后台任务，立即返回 Task 供上层接 run_id。"""
        run_id = request.run_id or self._run_id_factory()
        request.run_id = run_id
        task = asyncio.create_task(self.run(request), name=f"run:{run_id}")
        self._run_tasks[run_id] = task
        task.add_done_callback(lambda _t: self._run_tasks.pop(run_id, None))
        return task

    async def cancel(self, run_id: str) -> bool:
        task = self._run_tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    @property
    def active_run_ids(self) -> list[str]:
        return sorted(self._run_tasks)

    # ------------------------------------------------------------ 图执行

    async def _execute_graph(
        self,
        workflow: WorkflowDef,
        ctx: RunContext,
        record: "_RunRecord",
        *,
        is_child: bool,
    ) -> list[NodeExecution]:
        """按就绪队列推进整张图。

        先做一次校验：所有绑定来源必须是祖先，否则在这里就失败，而不是等到运行时
        才报「节点未产出结果」。
        """
        nodes = workflow.node_map
        remaining: dict[str, set[str]] = {}
        successors = workflow.successors()
        for node_id, node in nodes.items():
            remaining[node_id] = set(node.predecessors)

        executions: list[NodeExecution] = []
        record.executions = executions
        ready: list[str] = [n for n in nodes if not remaining[n]]
        ready.sort(key=lambda n: workflow.dependency_order_safe().index(n))

        running: dict[str, asyncio.Task[NodeExecution]] = {}
        failed_upstream: set[str] = set()

        while ready or running:
            while ready:
                node_id = ready.pop(0)
                node = nodes[node_id]
                record.execution_started(node)
                task = asyncio.create_task(
                    self._execute_node(node, ctx, record, is_child=is_child),
                    name=f"node:{node_id}",
                )
                running[node_id] = task

            if not running:
                break

            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                node_id = task.get_name().split(":", 1)[1]
                running.pop(node_id, None)
                execution = task.result()
                executions.append(execution)

                if not execution.succeeded:
                    failed_upstream.add(node_id)
                    await self._block_descendants(
                        node_id, successors, remaining, failed_upstream, executions,
                        workflow, record,
                    )
                    continue

                ctx.record(
                    NodeResult(
                        node_id=node_id,
                        outputs=dict(execution.outputs),
                        status=execution.status,
                    )
                )
                for nxt in successors.get(node_id, ()):
                    if nxt in failed_upstream:
                        continue
                    remaining[nxt].discard(node_id)
                    if not remaining[nxt] and nxt not in running:
                        ready.append(nxt)

        return executions

    async def _block_descendants(
        self,
        failed_node_id: str,
        successors: Mapping[str, set[str]],
        remaining: Mapping[str, set[str]],
        failed_upstream: set[str],
        executions: list[NodeExecution],
        workflow: WorkflowDef,
        record: "_RunRecord",
    ) -> None:
        """把失败节点的下游整片标记为 ``blocked``。

        失败传播按规划 P2 的缺省处理：关键失败阻断下游与最终回写，但**保留已完成结果**
        （不清理已成功的节点输出），以便人工核查与「复用已保存结果重试」。
        """
        stack = list(successors.get(failed_node_id, ()))
        while stack:
            node_id = stack.pop()
            if node_id in failed_upstream:
                continue
            failed_upstream.add(node_id)
            node = workflow.node_map[node_id]
            execution = NodeExecution(
                node_id=node_id,
                title=node.title,
                node_type=node.node_type,
                status=NodeStatus.BLOCKED,
                error=f"前置节点 {failed_node_id} 失败，本节点未执行",
            )
            executions.append(execution)
            record.execution_started(node)
            await self.recorder.node_finished(record, execution)
            stack.extend(successors.get(node_id, ()))

    async def _execute_node(
        self,
        node: NodeDef,
        ctx: RunContext,
        record: "_RunRecord",
        *,
        is_child: bool,
    ) -> NodeExecution:
        """执行单个节点，异常一律转成失败结果。"""
        queued_ms = now_ms()
        execution = NodeExecution(
            node_id=node.node_id,
            title=node.title,
            node_type=node.node_type,
            status=NodeStatus.RUNNING,
            queued_at_ms=queued_ms,
        )
        started = self._clock()

        try:
            async with self._slot(is_child):
                if record is not None:
                    await self.recorder.node_started(record, node)
                executor = self.registry.get(node.node_type)
                raw = await executor(node, ctx, self.runtime)
                outcome = normalise_outcome(raw)
        except asyncio.CancelledError:
            execution.status = NodeStatus.FAILED
            execution.error = "节点执行被取消"
            execution.started_at_ms = int((self._clock() - started) * 1000)
            execution.duration_ms = int((self._clock() - started) * 1000)
            await self.recorder.node_finished(record, execution)
            raise
        except MissingValue as exc:
            execution.status = NodeStatus.FAILED
            execution.error = f"变量解析失败：{exc}"
        except NodeExecutionError as exc:
            execution.status = NodeStatus.FAILED
            execution.error = str(exc)
        except Exception as exc:  # 未预期的异常：记类型与消息，不吞
            execution.status = NodeStatus.FAILED
            execution.error = f"{type(exc).__name__}: {exc}"
        else:
            execution.status = NodeStatus.SUCCEEDED
            execution.outputs = dict(outcome.outputs)
            execution.branch = outcome.branch
            execution.sub_run_id = outcome.meta.get("sub_run_id")

        execution.started_at_ms = int((self._clock() - started) * 1000)
        execution.duration_ms = int((self._clock() - started) * 1000)
        await self.recorder.node_finished(record, execution)
        return execution

    def _slot(self, is_child: bool) -> Any:
        """取执行名额。子运行不再申请，避免与等待它的父节点互等。"""
        if is_child:
            return _NULL_SLOT
        return self._active_slot

    # ------------------------------------------------------------ 结果汇总

    def _summarise(
        self,
        workflow: WorkflowDef,
        ctx: RunContext,
        executions: Sequence[NodeExecution],
    ) -> tuple[str, dict[str, Any], str | None]:
        """判定运行状态与对外输出。"""
        blocked = [e for e in executions if e.status == NodeStatus.BLOCKED]
        failed = [e for e in executions if e.status == NodeStatus.FAILED]

        exits = list(workflow.exits or [
            n.node_id for n in workflow.nodes if n.node_type == "end"
        ])
        outputs: dict[str, Any] = {}
        for exit_id in exits:
            if ctx.has(exit_id):
                outputs.update(ctx.result_of(exit_id).outputs)

        if failed:
            return (
                RunStatus.FAILED,
                outputs,
                f"{len(failed)} 个节点失败，{len(blocked)} 个节点被阻断；"
                f"首个失败：{failed[0].node_id} {failed[0].error}",
            )
        if blocked:
            return (
                RunStatus.FAILED,
                outputs,
                f"{len(blocked)} 个节点因前置失败被阻断",
            )
        if not outputs and exits:
            return (
                RunStatus.FAILED,
                outputs,
                f"出口节点 {exits} 均未产出结果",
            )
        return RunStatus.SUCCEEDED, outputs, None


class _RunRecord:
    """传给留痕层的运行记录句柄。字段刻意保持朴素，便于直接落库。"""

    __slots__ = (
        "run_id",
        "workflow_id",
        "workflow_display_name",
        "inputs",
        "call_path",
        "parent_run_id",
        "parent_node_id",
        "business_ref",
        "workflow",
        "executions",
        "started_at_ms",
        "node_started_at",
    )

    def __init__(self, request: RunRequest, workflow: WorkflowDef, run_id: str) -> None:
        self.run_id = run_id
        self.workflow_id = workflow.workflow_id
        self.workflow_display_name = workflow.display_name
        self.inputs = dict(request.inputs)
        self.call_path = request.call_path
        self.parent_run_id = request.parent_run_id
        self.parent_node_id = request.parent_node_id
        self.business_ref = request.business_ref
        self.workflow = workflow
        self.executions: list[NodeExecution] = []
        self.started_at_ms = now_ms()
        self.node_started_at: dict[str, int] = {}

    def execution_started(self, node: NodeDef) -> None:
        self.node_started_at.setdefault(node.node_id, now_ms())


class _NullSlot:
    """子运行使用的空名额：进入不等待，退出不释放。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


_NULL_SLOT = _NullSlot()


def now_ms() -> int:
    return int(time.time() * 1000)


def _default_run_id() -> str:
    import uuid

    return uuid.uuid4().hex

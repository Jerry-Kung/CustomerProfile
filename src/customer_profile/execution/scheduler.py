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
from .executors_iteration import ITERATION_ID_KEY
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

    call_path: str = ""
    """本节点执行时的调用路径。

    父图节点等于所在运行的路径；**迭代循环体节点**会被覆盖为
    ``<父路径>/iter:<iteration_id>/<index>``，否则同一静态节点 ID 的多轮执行会撞上
    ``node_executions`` 的唯一键 ``(run_id, node_id, call_path)``，被压成一条记录。
    """

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
        self._current_record: "_RunRecord | None" = None
        # 迭代执行器需要「按节点执行」这个入口，但它的签名里只有 runtime，拿不到调度器。
        # 因此把入口挂到 runtime 上，避免为这一处引入循环导入。
        self.runtime.iteration_node_runner = self.run_iteration_body_node

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
        ctx.bind_workflow(workflow)
        record = _RunRecord(request=request, workflow=workflow, run_id=run_id)
        self._contexts[run_id] = ctx
        self._current_record = record
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
            self._current_record = None
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
        self._current_record = None
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
        # 迭代循环体节点（带 iteration_id）不参与父图调度：它们挂在迭代节点内部，
        # 由 iteration 执行器逐项驱动。若在这里照常按图跑一遍，同一节点会被执行两次，
        # 且第二次读不到当前迭代项。
        body_ids = {n.node_id for n in workflow.nodes if n.config.get(ITERATION_ID_KEY)}
        if body_ids:
            nodes = {k: v for k, v in nodes.items() if k not in body_ids}
        remaining: dict[str, set[str]] = {}
        successors = workflow.successors()
        # 分支门槛：某个前置是 if-else 且本节点只挂在其中一支上时，另一支走完这条边
        # 就永远不会来。不判定这一点，两条分支会同时执行（真实冒烟时实测到）。
        gates: dict[str, dict[str, str]] = {
            node_id: node.branch_gates_map
            for node_id, node in nodes.items()
            if node.branch_gates
        }
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
                    required = gates.get(nxt, {}).get(node_id)
                    if required is not None and required != execution.branch:
                        # 本节点走了另一支：这条边永远不会来。把该后继整棵子树标为
                        # SKIPPED，并让它自己的后继也丢掉这条边。
                        await self._skip_branch(
                            nxt, node_id, successors, remaining, failed_upstream,
                            executions, workflow, record,
                        )
                        continue
                    remaining[nxt].discard(node_id)
                    if not remaining[nxt] and nxt not in running:
                        ready.append(nxt)

        return executions

    async def _skip_branch(
        self,
        node_id: str,
        from_node_id: str,
        successors: Mapping[str, set[str]],
        remaining: Mapping[str, set[str]],
        failed_upstream: set[str],
        executions: list[NodeExecution],
        workflow: WorkflowDef,
        record: "_RunRecord",
    ) -> None:
        """把「未走到的那一支」整棵子树标为 ``SKIPPED``。

        与 :meth:`_block_descendants` 的区别：``BLOCKED`` 是**故障**（前置失败、结果不可信），
        ``SKIPPED`` 是**没走这条路**（正常分支选择），因此前者让运行失败、后者不影响成败。

        父分支的另一支与其汇合点（典型是 ``variable-aggregator``）仍须执行——汇合点不会
        因为一支没走就不产出，所以这里只丢掉「这条边」，并在此后按「全部前置都已失效」
        判定是否连带跳过。
        """
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in failed_upstream:
                continue
            failed_upstream.add(current)
            node = workflow.node_map[current]
            execution = NodeExecution(
                node_id=current,
                title=node.title,
                node_type=node.node_type,
                status=NodeStatus.SKIPPED,
                error=f"前置 {from_node_id} 未走本分支，本节点未执行",
            )
            executions.append(execution)
            record.execution_started(node)
            await self.recorder.node_finished(record, execution)
            for nxt in successors.get(current, ()):
                remaining[nxt].discard(current)
                # 汇合点仍有其它前置时留给调度循环按就绪判定；这里只处理「一支独苗」
                if not remaining[nxt]:
                    stack.append(nxt)

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

    async def run_iteration_body_node(
        self,
        node: NodeDef,
        item_ctx: RunContext,
        *,
        iteration_id: str,
        index: int,
    ) -> NodeExecution:
        """执行一次迭代循环体节点。

        留痕的 ``call_path`` 覆盖为 ``<父路径>/iter:<iteration_id>/<index>``：同一静态
        节点 ID 每轮都会再执行一次，不区分的话 ``node_executions`` 的唯一键
        ``(run_id, node_id, call_path)`` 会把多轮记录压成一条，迭代的逐项留痕就没了。
        """
        record = self._current_record
        if record is None:
            raise RuntimeError("run_iteration_body_node 必须在一次运行内调用")
        override = f"{record.call_path}/iter:{iteration_id}/{index}"
        execution = await self._execute_node(
            node, item_ctx, record, is_child=True, call_path_override=override
        )
        # 循环体不受父图调度（见 _execute_graph 里对 body_ids 的过滤），因此它的执行记录
        # 不会经过那里的 append。这里补上：否则数据库有逐轮留痕、而内存结果里一条都看不到，
        # 「迭代逐项可追溯」在返回值这一侧就是断的。
        record.executions.append(execution)
        return execution

    async def _execute_node(
        self,
        node: NodeDef,
        ctx: RunContext,
        record: "_RunRecord",
        *,
        is_child: bool,
        call_path_override: str | None = None,
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
        started_ms = now_ms()
        """节点的**墙钟**开始时间。

        与 ``started``（单调钟）分开：``duration_ms`` 必须用单调钟算，墙钟会被系统时间
        调整影响、算出负时长；而时间线要的是墙钟——只有墙钟才能和运行行、其它节点的
        时间放在同一条轴上比较。两者此前被写成同一行表达式，``started_at_ms`` 因此等于
        ``duration_ms``，时间线无从绘制（V0.4.4 实测发现）。
        """
        # 留痕时使用覆盖后的调用路径。记录句柄是每次运行一份、循环体串行执行，
        # 因此临时替换再还原是安全的。
        original_call_path = record.call_path
        if call_path_override is not None:
            record.call_path = call_path_override

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
            execution.started_at_ms = started_ms
            execution.duration_ms = int((self._clock() - started) * 1000)
            if call_path_override:
                execution.call_path = call_path_override
            await self.recorder.node_finished(record, execution)
            record.call_path = original_call_path
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
            execution.call_path = record.call_path if call_path_override else ""

        execution.started_at_ms = started_ms
        execution.duration_ms = int((self._clock() - started) * 1000)
        if call_path_override:
            execution.call_path = call_path_override
        await self.recorder.node_finished(record, execution)
        record.call_path = original_call_path
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

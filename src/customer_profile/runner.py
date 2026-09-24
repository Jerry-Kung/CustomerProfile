"""把各层装配成一个可用的执行服务。

上面各模块互相独立，本模块是唯一把它们接起来的地方：定义 → 调度 → 执行 → 落库。
API 层只调用这里，不自己拼装依赖。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from .definitions import WorkflowDef
from .execution import code_registry
from .execution.executors import ExecutorRegistry, NodeRuntime, build_default_registry
from .execution.http import HttpClient
from .execution.llm import LlmClient
from .execution.scheduler import RunOutcome, RunRequest, Scheduler
from .execution.subworkflow import register_tool_executor
from .layout import attach_layout
from .persistence import RunTracker
from .persistence.store import Store
from .replay import ReplaySource, http_key
from .settings import Settings, get_settings
from .templates import TemplateRepository
from .validation import validate

MAX_ACTIVE_NODES_DEFAULT = 8


@dataclass
class Service:
    """一次进程内的执行服务。"""

    settings: Settings
    registry: ExecutorRegistry
    definitions: dict[str, WorkflowDef]
    runtime: NodeRuntime
    store: Store | None = None
    tracker: RunTracker | None = None
    replay: ReplaySource | None = None
    scheduler: Scheduler | None = None
    llm_client: LlmClient | None = None
    http_client: HttpClient | None = None
    run_slots: "RunSlotGate | None" = None
    """同时运行的工作流数上限（``MAX_ACTIVE_RUNS``）。见 :class:`RunSlotGate`。"""
    issue_report: dict[str, list[str]] = field(default_factory=dict)
    """校验发现，按工作流 ID 归档。error 级发现的工作流不进入 ``definitions``。"""

    # ------------------------------------------------------------ 运行

    async def run(
        self,
        workflow_id: str,
        inputs: Mapping[str, Any],
        *,
        business_ref: str | None = None,
    ) -> RunOutcome:
        """同步执行一个工作流，返回完整结果。"""
        if self.scheduler is None:
            raise RuntimeError("服务尚未 initialize()")
        workflow = self.require_workflow(workflow_id)
        return await self.scheduler.run(
            RunRequest(
                workflow=workflow,
                inputs=dict(inputs),
                business_ref=business_ref,
            )
        )

    async def submit(
        self,
        workflow_id: str,
        inputs: Mapping[str, Any],
        *,
        business_ref: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """异步提交一个工作流，立即返回 ``run_id``。

        名额已满时抛 :class:`RunSlotUnavailable`——**明确拒绝，不排队**。排队会让调用方
        拿到一个 run_id 却迟迟不开始，看上去像提交失败；拒绝则是可解释的即时反馈。
        """
        if self.scheduler is None:
            raise RuntimeError("服务尚未 initialize()")
        workflow = self.require_workflow(workflow_id)
        request = RunRequest(
            workflow=workflow,
            inputs=dict(inputs),
            business_ref=business_ref,
            run_id=run_id or "",
        )
        if self.run_slots is not None:
            self.run_slots.acquire()
        try:
            task = await self.scheduler.submit(request)
        except BaseException:
            # 提交本身失败（或协程被取消）时名额必须还回去，否则会永久泄漏一个并发位。
            if self.run_slots is not None:
                self.run_slots.release()
            raise
        task.add_done_callback(lambda _t: self.run_slots and self.run_slots.release())
        return task.get_name().split(":", 1)[1]

    async def wait_until_recorded(self, run_id: str, timeout: float = 5.0) -> bool:
        """等到该运行的记录出现在库里，返回是否等到。

        用于「提交后立刻可查」的契约。超时不抛异常而是返回 ``False``：调用方已经拿到
        run_id，此时报错会把一次成功提交变成失败；但也不能假装已落库，因此返回布尔值
        让上层自行决定（当前上层只在拿到 True 时继续）。
        """
        if self.store is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.store.get_run(run_id) is not None:
                return True
            await asyncio.sleep(0.01)
        return False

    async def aclose(self) -> None:
        if self.llm_client is not None:
            await self.llm_client.aclose()
        if self.http_client is not None:
            await self.http_client.aclose()

    # ------------------------------------------------------------ 定义

    def require_workflow(self, workflow_id: str) -> WorkflowDef:
        try:
            return self.definitions[workflow_id]
        except KeyError as exc:
            raise KeyError(
                f"工作流 {workflow_id!r} 不可用；可用的：{sorted(self.definitions)}"
            ) from exc

    def topology(self, workflow_id: str) -> dict[str, Any]:
        """导出拓扑，供只读前端使用（V0.4 复用同一份定义，不维护第二张图）。

        ``asdict()`` 的 ``coords`` 保持原样（声明），算出来的展示坐标放在独立的
        ``layout`` 字段。两者分开：一个是人定的，一个是算的，混在一起就分不清了。
        """
        workflow = self.require_workflow(workflow_id)
        return attach_layout(workflow.asdict(), workflow)



class RunSlotUnavailable(RuntimeError):
    """同时运行的工作流数已达上限，本次提交被拒绝。"""

    def __init__(self, limit: int, active: int) -> None:
        self.limit = limit
        self.active = active
        super().__init__(
            f"同时运行的工作流数已达上限（{active}/{limit}）；请等已有运行结束后再提交"
        )


class RunSlotGate:
    """同时运行的**工作流数**上限，取值来自 ``MAX_ACTIVE_RUNS``。

    与 :class:`~customer_profile.execution.scheduler.Scheduler` 的 ``active_slot`` 是两件事：

    - ``active_slot``（``MAX_ACTIVE_NODES``）限制**一次运行内部**同时执行的节点数；
    - 本闸门限制**同时跑多少个运行**，与 HTTP 形态无关，因此放在服务层，对 CLI 与 API 同样生效。

    两者都不是速率限制（RPM/TPM 属 V0.5 生产保障，本次不做）。

    **用计数器而不是** ``asyncio.Semaphore``：这里的申请是严格非阻塞的——满了就立刻抛
    :class:`RunSlotUnavailable`，从不等待。``Semaphore`` 没有非阻塞的 ``acquire``，要用它就得
    去动 ``_value`` 私有属性；既然不需要排队，一个整数就够，也少一处依赖实现细节的地方。

    **子运行不经过这里**：它们由 ``SubWorkflowRunner`` 直接调 ``Scheduler.run``，不走
    ``Service.submit``。这是刻意的——主入口一次运行会扇出十几个子运行，若子运行也申请名额，
    上限一低就会自锁。
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"运行名额上限至少为 1，实际为 {limit}")
        self.limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        """当前占用名额的运行数。"""
        return self._active

    @property
    def available(self) -> int:
        return max(0, self.limit - self._active)

    def acquire(self) -> None:
        """申请一个名额；无空闲时抛 :class:`RunSlotUnavailable`。不阻塞。"""
        if self._active >= self.limit:
            raise RunSlotUnavailable(self.limit, self._active)
        self._active += 1

    def release(self) -> None:
        """归还一个名额。

        幂等：``_active`` 掉到 0 以下会让闸门**永久多放行**若干运行，比少放行危险得多，
        因此这里对多余释放直接忽略。
        """
        if self._active <= 0:
            return
        self._active -= 1

async def build_service(
    settings: Settings | None = None,
    *,
    replay: ReplaySource | None = None,
    enable_store: bool = True,
    strict_replay: bool | None = None,
    max_active_nodes: int = MAX_ACTIVE_NODES_DEFAULT,
) -> Service:
    """装配执行服务。

    ``replay`` 给出时，LLM 与 HTTP 全部走固定响应，不出网。回放模式下**任何写请求
    都不会真的发出**，符合「测试与影子模式禁止生产回写」的要求。
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    if replay is None and settings.is_replaying:
        if not settings.fixture_dir.is_dir():
            raise FileNotFoundError(
                f"REPLAY_MODE=fixture 但 fixture 目录不存在：{settings.fixture_dir}"
            )
        replay = ReplaySource.from_directory(
            settings.fixture_dir,
            strict=settings.replay_strict if strict_replay is None else strict_replay,
        )
    elif replay is not None and strict_replay is not None:
        replay.strict = strict_replay

    code_registry.autodiscover()

    import asyncio

    request_limiter = asyncio.Semaphore(max(1, settings.max_concurrent_requests))

    transport = _build_transport(replay)
    llm_client = LlmClient(
        settings,
        transport=transport,
        replay=replay,
        request_limiter=request_limiter,
    )
    http_client = HttpClient(
        settings,
        transport=transport,
        replay=replay,
        request_limiter=request_limiter,
    )

    store: Store | None = None
    tracker: RunTracker | None = None
    if enable_store:
        store = Store(_database_path(settings))
        store.connect()
        if settings.interrupt_on_start:
            await store.mark_running_as_interrupted()
        tracker = RunTracker(store, is_replay=replay is not None, code_commit=_code_commit())

    runtime = NodeRuntime(
        settings=settings,
        request_limiter=request_limiter,
        llm=llm_client,
        http=http_client,
        recorder=tracker,
        extra={"template_repository": TemplateRepository()},
    )
    registry = build_default_registry()

    definitions, issues = _load_definitions()

    run_slots = (
        RunSlotGate(max(1, settings.max_active_runs))
        if settings.max_active_runs is not None
        else None
    )

    scheduler = Scheduler(
        registry,
        runtime,
        max_active_nodes=max_active_nodes,
        recorder=tracker,
    )
    register_tool_executor(registry, runtime, definitions)

    if tracker is not None:
        llm_client._recorder = tracker.attempt_recorded
        http_client._recorder = tracker.attempt_recorded

    service = Service(
        settings=settings,
        registry=registry,
        definitions=definitions,
        runtime=runtime,
        store=store,
        tracker=tracker,
        replay=replay,
        scheduler=scheduler,
        llm_client=llm_client,
        http_client=http_client,
        run_slots=run_slots,
        issue_report=issues,
    )
    # tracker 需要访问调度器的运行上下文以快照入参
    if tracker is not None:
        tracker.scheduler = scheduler
    return service


def _load_definitions() -> tuple[dict[str, WorkflowDef], dict[str, list[str]]]:
    """加载全部工作流定义，按校验结果分流。

    error 级发现的工作流**不进入可运行集合**，但把问题记进 ``issue_report``：
    静默跳过会让「少了一个工作流」变成运行期才发现的怪问题。
    """
    from . import workflows

    available: dict[str, WorkflowDef] = {}
    issues: dict[str, list[str]] = {}

    for workflow_id, definition in workflows.load_all().items():
        found = validate(definition)
        messages = [str(i) for i in found]
        if messages:
            issues[workflow_id] = messages
        if any(i.level == "error" for i in found):
            continue
        available[workflow_id] = definition.normalise()
    return available, issues


def _build_transport(replay: ReplaySource | None) -> Any:
    """回放模式下构造一个不会出网的 httpx transport。

    它兜底失败：走到这里说明 ``replay.next_http`` 没命中，而严格模式已经在客户端
    抛出更清楚的错误。这里返回 599 而不是真实请求，确保回放模式下不可能出网。
    """
    if replay is None:
        return None

    headers = {"content-type": "application/json"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            599,
            headers=headers,
            json={
                "error": "replay_transport",
                "detail": f"回放模式未匹配到固定响应：{request.method} {request.url}",
            },
        )

    return httpx.MockTransport(handler)


def _database_path(settings: Settings) -> Path:
    path = settings.sqlite_path
    if path is None:
        raise ValueError(
            f"V0.2 仅支持 SQLite，当前 DATABASE_URL={settings.database_url!r}"
        )
    return path


def _code_commit() -> str | None:
    """取当前提交号，写进版本快照。取不到时返回 ``None``，不伪造。"""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def replay_key_for_llm(payload: Mapping[str, Any]) -> str:
    """对外暴露的 LLM fixture 键计算入口，供脚本与测试使用。"""
    from .replay import llm_fingerprint

    return llm_fingerprint(payload)


__all__ = [
    "RunSlotGate",
    "RunSlotUnavailable",
    "Service",
    "build_service",
    "replay_key_for_llm",
    "http_key",
]

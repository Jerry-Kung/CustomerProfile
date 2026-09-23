"""最小 HTTP API。

范围刻意收窄到 V0.2 需要的五个端点（规划 §3 V0.2 交付物 5）：提交任务返回 run_id、
查询运行状态与节点明细。**没有**前端、没有编辑、没有流式推送——那些属 V0.4/V0.5。

调用方协议仍是 Q7 的缺省处理：只提供异步提交 + 查询 run_id。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .definitions import NodeDef, RunStatus, WorkflowDef
from .layout import layout
from .runner import Service, build_service
from .settings import get_settings


class SubmitRequest(BaseModel):
    """提交一次工作流运行。"""

    workflow_id: str = Field(..., description="工作流 ID，见 GET /workflows")
    inputs: dict[str, Any] = Field(default_factory=dict, description="工作流入参")
    business_ref: str | None = Field(
        default=None, description="业务标识（如客户手机号），用于列表检索"
    )


class SubmitResponse(BaseModel):
    run_id: str
    workflow_id: str


class RunSummary(BaseModel):
    run_id: str
    workflow_id: str
    status: str
    business_ref: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    is_replay: bool = False


class RunDetail(RunSummary):
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    definition_version_id: int | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    child_run_ids: list[str] = Field(default_factory=list)


class RunGraph(BaseModel):
    """运行详情页的主数据：当次版本的图 + 节点状态 + 子运行映射。

    图与状态分开两个字段，不合并成「带状态的节点列表」：图属定义层（可能与当前代码
    不同版本），状态属运行层。合并后「这个节点在图里存在但没执行」和「执行了但已不在
    图里」会无从分辨。
    """

    run: RunSummary
    definition_version_id: int | None = None
    definition: dict[str, Any] = Field(
        default_factory=dict, description="当次定义快照，含 layout 字段"
    )
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    child_runs: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="触发节点 ID → 子运行摘要"
    )
    is_replay: bool = False


def create_app(service_factory: Any = None) -> FastAPI:
    """构造 FastAPI 应用。

    ``service_factory`` 可注入，便于测试拿一个用固定响应装配的服务。缺省在
    lifespan 里用环境配置装配真实的那个。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service_factory is not None:
            app.state.service = await service_factory()
        else:
            app.state.service = await build_service(get_settings())
        try:
            yield
        finally:
            # 关停顺序很重要：提交的运行为后台任务，若先关存储，仍在跑的运行会写进
            # 一个已关闭的连接（现象是「Store 尚未 connect()」，且该运行查不到）。
            # 因此先等在飞运行收敛，再关闭存储。
            service = getattr(app.state, "service", None)
            if service is not None:
                await _drain_running_tasks(service)
                await service.aclose()
                if service.store is not None:
                    service.store.close()

    description = "潜在目标客户人设画像分析工作流 —— 最小执行器与留痕 API"

    app = FastAPI(
        title="Customer Profile",
        version="0.4.1",
        description=description,
        lifespan=lifespan,
    )

    async def _drain_running_tasks(service: Service, timeout: float = 60.0) -> None:
        """等在飞的后台运行结束。

        超时后放弃等待并留下可见记录，不静默丢弃：退出阶段把一个仍在跑的运行抛下，
        会留下无法解释的中间状态。
        """
        scheduler = service.scheduler
        if scheduler is None:
            return
        tasks = list(scheduler._run_tasks.values())  # noqa: SLF001 — 同一模块族的内部协作
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            print(
                f"[关停] {len(pending)} 个后台运行在 {timeout}s 内未结束，已请求取消",
                file=sys.stderr,
                flush=True,
            )
        for task in done:
            # 取出异常，避免 asyncio 打出「Task exception was never retrieved」
            if not task.cancelled():
                task.exception()

    def get_service() -> Service:
        service = getattr(app.state, "service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="服务尚未就绪")
        return service

    # ------------------------------------------------------------ 元信息

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        """健康检查。顺带报告回放模式与定义校验发现，避免「看起来正常」。"""
        service = get_service()
        return {
            "status": "ok",
            "replay_mode": service.settings.replay_mode,
            "workflows": sorted(service.definitions),
            "definition_issues": service.issue_report,
        }

    @app.get("/workflows", tags=["meta"])
    async def list_workflows() -> list[dict[str, Any]]:
        """列出可运行的工作流及其拓扑（前端与调度器共用这一份定义）。"""
        service = get_service()
        return [
            {
                "workflow_id": definition.workflow_id,
                "display_name": definition.display_name,
                "source_dsl": definition.source_dsl,
                "node_count": len(definition.nodes),
                "outputs": dict(definition.outputs),
            }
            for definition in service.definitions.values()
        ]

    @app.get("/workflows/{workflow_id}/topology", tags=["meta"])
    async def workflow_topology(workflow_id: str) -> dict[str, Any]:
        service = get_service()
        try:
            return service.topology(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/definition-versions/{version_id}", tags=["meta"])
    async def definition_version(version_id: int) -> dict[str, Any]:
        """取一份定义快照，并附加布局坐标。

        运行详情展示的是**当时的**图与提示词版本，因此前端取图走这里而不是当前定义。
        快照已在每次运行开始时由留痕层写入（``definition_versions`` 表）。

        返回形状与 ``/runs/{id}/graph`` 一致——图**嵌套**在 ``definition`` 键下。
        两个端点返回同一类东西，形状不同会让前端出现两套取图代码；嵌套也让
        「快照缺失时 ``definition`` 为空字典」这一情形有稳定的形状可断言。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        row = await service.store.definition_version(version_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"定义版本 {version_id} 不存在"
            )
        return {
            "version_id": version_id,
            "workflow_id": row.get("workflow_id"),
            "definition_hash": row.get("definition_hash"),
            "code_commit": row.get("code_commit"),
            "created_at_ms": row.get("created_at_ms"),
            "definition": _with_layout(row, service),
        }

    # ------------------------------------------------------------ 运行

    @app.post("/runs", response_model=SubmitResponse, tags=["runs"])
    async def submit_run(payload: SubmitRequest = Body(...)) -> SubmitResponse:
        """提交任务，立即返回 ``run_id``。

        任务先落库再返回（`Dify迁移任务说明.md` §4），因此不会出现「拿到 run_id 但
        数据库里查不到」的情况。
        """
        service = get_service()
        try:
            run_id = await service.submit(
                payload.workflow_id,
                payload.inputs,
                business_ref=payload.business_ref,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # 等运行记录真正落库再返回。「先落库再返回 run_id」这条要求不能只靠「任务已
        # 创建」来满足——调用方拿到 ID 立刻查询也必须查得到，否则契约不成立。
        if service.store is not None:
            await service.wait_until_recorded(run_id)
        return SubmitResponse(run_id=run_id, workflow_id=payload.workflow_id)

    @app.get("/runs", response_model=list[RunSummary], tags=["runs"])
    async def list_runs(
        workflow_id: str | None = None,
        status: str | None = None,
        business_ref: str | None = None,
        include_children: bool = False,
        created_after_ms: int | None = None,
        created_before_ms: int | None = None,
        order_by: str = "created_desc",
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[RunSummary]:
        """任务列表。返回**裸数组**，总数走 ``GET /runs/count``。

        刻意不改成 ``{items, total}`` 信封：现有调用方与测试都按数组取（`test_api.py`），
        改形状是破坏性变更，而分页所需的只是多一个数字。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        rows = await service.store.list_runs(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=include_children,
            created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        return [_summary_of(row) for row in rows]

    @app.get("/runs/count", tags=["runs"])
    async def count_runs(
        workflow_id: str | None = None,
        status: str | None = None,
        business_ref: str | None = None,
        include_children: bool = False,
        created_after_ms: int | None = None,
        created_before_ms: int | None = None,
    ) -> dict[str, int]:
        """满足**同一组筛选条件**的运行总数，供分页控件算页数。

        必须与 ``GET /runs`` 用同一组参数：条件分叉会让总页数与实际数据不匹配。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        total = await service.store.count_runs(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=include_children,
            created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
        )
        return {"total": total}

    @app.get("/runs/{run_id}", response_model=RunDetail, tags=["runs"])
    async def get_run(run_id: str) -> RunDetail:
        """运行详情：节点明细与每次外部尝试。

        历史运行展示的是**当时的定义版本**（``definition_version_id``），新版本不会
        覆盖旧运行的展示。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        row = await service.store.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")

        nodes = await service.store.list_node_executions(run_id)
        attempts = await service.store.list_attempts(run_id)
        children = await service.store.list_child_runs(run_id)
        return RunDetail(
            **_summary_of(row).model_dump(),
            inputs=row.get("inputs_json") or {},
            outputs=row.get("outputs_json") or {},
            definition_version_id=row.get("definition_version_id"),
            nodes=nodes,
            attempts=attempts,
            child_run_ids=[child["run_id"] for child in children],
        )

    @app.get("/runs/{run_id}/graph", response_model=RunGraph, tags=["runs"])
    async def get_run_graph(run_id: str) -> RunGraph:
        """运行详情页的主数据：当次版本的图 + 节点状态 + 子运行映射。

        一次返回，避免前端为了同一张图做三次往返（拿运行、拿图、拿子运行）。
        图来自 ``definition_versions`` 快照，不是当前定义——否则历史运行会被新版本覆盖。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        row = await service.store.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")

        version_id = row.get("definition_version_id")
        definition: dict[str, Any] = {}
        if version_id is not None:
            version_row = await service.store.definition_version(version_id)
            if version_row is not None:
                definition = _with_layout(version_row, service)

        nodes = await service.store.list_node_executions(run_id)
        children = await service.store.list_child_runs(run_id)

        # 子运行按「触发它的父节点」索引，前端点节点即可下钻，不必自己再查一次。
        child_by_node: dict[str, dict[str, Any]] = {}
        for child in children:
            parent_node = child.get("parent_node_id")
            if parent_node:
                child_by_node[parent_node] = {
                    "run_id": child["run_id"],
                    "workflow_id": child.get("workflow_id"),
                    "status": child.get("status"),
                    "duration_ms": child.get("duration_ms"),
                }

        return RunGraph(
            run=_summary_of(row),
            definition_version_id=version_id,
            definition=definition,
            nodes=nodes,
            child_runs=child_by_node,
            is_replay=bool(row.get("is_replay")),
        )

    @app.get("/runs/{run_id}/nodes/{node_id}/attempts", tags=["runs"])
    async def node_attempts(
        run_id: str,
        node_id: str,
        call_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """某个节点的每一次外部请求尝试（V0.4.3 的「看到每次尝试」）。

        **按节点懒加载，不塞进 ``/graph``**：``response_text`` 可以很大（单次 LLM 响应
        上万字符，主入口跑一次有 17 处 LLM 调用），全部内联到详情页首屏会让页面为了
        一张图加载好几 MB。节点行里已有 ``attempt_count``，前端据此决定是否要拉。

        ``call_path`` 由节点行带回来，用于区分迭代各轮——只按 ``node_id`` 会把多轮尝试
        混在一起。不传则返回该节点全部轮的尝试。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        if await service.store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
        return await service.store.list_attempts(
            run_id, node_id=node_id, call_path=call_path
        )

    @app.post("/runs/{run_id}/cancel", tags=["runs"])
    async def cancel_run(run_id: str) -> dict[str, Any]:
        """请求取消一个仍在运行的运行。"""
        service = get_service()
        if service.scheduler is None:
            raise HTTPException(status_code=503, detail="服务尚未就绪")
        cancelled = await service.scheduler.cancel(run_id)
        if not cancelled:
            raise HTTPException(
                status_code=409, detail=f"运行 {run_id} 不在进行中，无法取消"
            )
        return {"run_id": run_id, "cancelled": True}

    # 静态挂载最后加：放在所有 API 路由注册之后，避免 ``/`` 的前缀匹配盖住 API。
    if service_factory is None:
        _mount_ui(app, get_settings())

    return app


def _mount_ui(app: FastAPI, settings: Any) -> None:
    """把前端构建产物挂到根路径。未构建或未开启时安静跳过。

    **不做成启动失败**：没有前端是合法状态（只跑 API、跑测试、未装 node），
    让它把服务拖住起不来是把可选功能变成了硬依赖。
    """
    if not settings.serve_ui:
        return
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        print(
            f"[前端] SERVE_UI=true 但未找到构建产物：{dist}；"
            f"请先执行 cd frontend && npm run build",
            file=sys.stderr,
            flush=True,
        )
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")


def _summary_of(row: Mapping[str, Any]) -> RunSummary:
    return RunSummary(
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        status=row.get("status") or RunStatus.QUEUED,
        business_ref=row.get("business_ref"),
        started_at_ms=row.get("started_at_ms"),
        finished_at_ms=row.get("finished_at_ms"),
        duration_ms=row.get("duration_ms"),
        error=row.get("error"),
        is_replay=bool(row.get("is_replay")),
    )


def _with_layout(
    version_row: Mapping[str, Any], service: Service
) -> dict[str, Any]:
    """给一份**历史定义快照**附加布局坐标。

    快照是经 JSON 往返的普通 dict，不是 :class:`~customer_profile.WorkflowDef`，因此
    不能直接复用 :func:`layout.attach_layout`（它按 ``NodeDef`` 工作）。这里从 dict 取
    出分层所需的最小信息（节点 ID 与前置关系），复用同一个分层函数算坐标。

    只有 ``predecessors`` 参与分层，不用 ``bindings``：展示坐标不关心变量绑定。
    """
    definition = dict(version_row.get("definition") or {})
    nodes = definition.get("nodes") or []

    # 用 NodeDef 的最小投影喂给分层函数，避免为 dict 再写一套分层逻辑。
    projected = WorkflowDef(
        workflow_id=definition.get("workflow_id") or "",
        display_name=definition.get("display_name") or "",
        nodes=tuple(
            NodeDef(
                node_id=n["node_id"],
                title=n.get("title") or n["node_id"],
                node_type=n.get("type") or "code",
                predecessors=tuple(n.get("direct_predecessors") or ()),
                coords=(
                    tuple(n["coords"])  # type: ignore[arg-type]
                    if n.get("coords")
                    else None
                ),
            )
            for n in nodes
            if n.get("node_id")
        ),
    )
    definition["layout"] = {
        node_id: [round(x, 2), round(y, 2)]
        for node_id, (x, y) in layout(projected).items()
    }
    return definition


app = create_app()

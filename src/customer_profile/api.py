"""最小 HTTP API。

范围刻意收窄到 V0.2 需要的五个端点（规划 §3 V0.2 交付物 5）：提交任务返回 run_id、
查询运行状态与节点明细。**没有**前端、没有编辑、没有流式推送——那些属 V0.4/V0.5。

调用方协议仍是 Q7 的缺省处理：只提供异步提交 + 查询 run_id。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any, Mapping

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .definitions import RunStatus
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

    app = FastAPI(
        title="Customer Profile",
        version="0.2.0",
        description="潜在目标客户人设画像分析工作流 —— 最小执行器与留痕 API",
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
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[RunSummary]:
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        rows = await service.store.list_runs(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=include_children,
            limit=limit,
            offset=offset,
        )
        return [_summary_of(row) for row in rows]

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

    return app


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


app = create_app()

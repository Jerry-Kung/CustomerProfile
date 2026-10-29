"""最小 HTTP API。

范围刻意收窄到 V0.2 需要的五个端点：提交任务返回 run_id、
查询运行状态与节点明细。**没有**前端、没有编辑、没有流式推送——那些属 V0.4/V0.5。

调用方协议仍是 Q7 的缺省处理：只提供异步提交 + 查询 run_id。
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from .definitions import RunStatus
from .logging_config import configure_logging
from .runner import QueueFull, RunSlotUnavailable, Service, build_service
from .settings import get_settings


ENTRY_WORKFLOW_ID = "customer_profile_pipeline"
"""主入口工作流 ID，即业务测试臂。手机号校验只对它启用，不把入口契约硬编码给全部工作流。

2026-10-16 由 ``customer_profile_entry``（客户初始画像，基线臂）改指本臂：该臂已从
``WORKFLOW_MODULES`` 摘除、不再可运行，而提交页与入参校验必须落在同一个 ID 上——
只改前端会让手机号校验静默失效。
"""

DEFAULT_BATCH_ID = "batch_001"
"""主入口未传 ``batch_id`` 时补的缺省批次号。

``start`` 节点把可选入参声明在 ``required_batch_id=False`` 上，但执行器只输出「声明了
且调用方传了」的字段——不传时该键根本不出现，下游取 ``(START, "batch_id")`` 直接
``KeyError``（实测：照着 README 的 curl 示例只传手机号，运行在 ``pipeline_production``
节点失败）。这里在提交入口补一个确定值，让「可选」真正可选。

只在提交入口补，不在 ``execute_start`` 里改：那里的语义是「原样暴露调用方入参」，
给所有工作流的所有可选入参凭空补值会改变其它工作流的既有行为。
"""

PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
"""中国大陆手机号。与 ``scripts/smoke_run.py`` 的脱敏正则同一形状，此处锚定整串。"""


def _validate_entry_inputs(workflow_id: str, inputs: Mapping[str, Any]) -> None:
    """在主入口上做提交前入参校验。

    此前**没有任何前置校验**：``start`` 节点的必填性在执行期才由 ``execute_start`` 检查，
    于是少传或传错手机号会变成一个失败运行，而不是一个可解释的 4xx。手机号还会进
    ``business_ref`` 与多个下游 HTTP 路径（``/data/history/{phone}``、``/data/profile/{phone}``），
    空串或带斜杠的值会在外部服务上变成难以归因的 404。

    只校验主入口：其它工作流（如 ``evidence_subagent``、录音类）的入参契约不同，
    在这里一并约束会把它们的合法调用挡掉。
    """
    if workflow_id != ENTRY_WORKFLOW_ID:
        return
    phone = inputs.get("phone_number")
    if phone is None:
        raise HTTPException(
            status_code=422, detail=f"{ENTRY_WORKFLOW_ID} 缺少必填入参 phone_number"
        )
    if not isinstance(phone, str) or not PHONE_PATTERN.match(phone):
        raise HTTPException(
            status_code=422,
            detail=f"phone_number 须为 11 位中国大陆手机号（1 开头，第二位 3-9），实际为 {phone!r}",
        )


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
        default_factory=dict, description="当次定义快照，取运行开始时落库的那一份"
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

    if service_factory is None:
        configure_logging(get_settings().log_level, component="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service_factory is not None:
            app.state.service = await service_factory()
        else:
            app.state.service = await build_service(get_settings())
        service = app.state.service
        logger.info(
            "API 服务已就绪，工作流数量={}，内联执行={}，回写启用={}",
            len(service.definitions), service.settings.inline_worker,
            service.settings.writeback_enabled,
        )
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
                logger.info("API 服务已关闭")

    description = "潜在目标客户人设画像分析工作流 —— 最小执行器与留痕 API"

    app = FastAPI(
        title="Customer Profile",
        version="0.5.4",
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
            logger.warning(
                "API 关停等待超时，{} 个后台运行在 {} 秒内未结束，已请求取消",
                len(pending), timeout,
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

    @app.get("/runtime", tags=["meta"])
    async def runtime() -> dict[str, Any]:
        """当前运行态，供运行台显示。

        **只报配置，不报凭据。** ``llm_model`` 是配置项而非密钥（``.env.example`` 已公开其
        占位），三个 API Key 一律不出现在返回值里。

        存在的理由：回写被 ``WRITEBACK_ENABLED=false`` 拦下时，节点只产出「已跳过」的结果，
        运行却仍显示 succeeded。没有这个端点，界面上「成功」会被读成「已写进生产库」，
        而实际上没有任何生产副作用——这正是 ``smoke_run.py`` 在报告里显式记 ``writeback_enabled``
        的理由，只是那个记录在 CLI 里，运行台上看不到。
        """
        service = get_service()
        settings = service.settings
        slots = service.run_slots
        # 队列快照取自库，不是内存：worker 可能在另一个进程里，本进程无从知道它领走了什么。
        queue = (
            await service.store.queue_snapshot() if service.store is not None else None
        )
        quota = service.quota.snapshot() if service.quota is not None else {}
        return {
            "replay_mode": settings.replay_mode,
            "writeback_enabled": settings.writeback_enabled,
            "llm_model": settings.llm_model,
            # 配额：只报开关与剩余额度，不含任何凭据。
            "llm_rpm_limit": settings.llm_rpm_limit,
            "llm_tpm_limit": settings.llm_tpm_limit,
            "worker_replicas": settings.worker_replicas,
            "quota": quota,
            "max_active_runs": settings.max_active_runs,
            "max_queued_runs": settings.max_queued_runs,
            "max_concurrent_requests": settings.max_concurrent_requests,
            "active_runs": slots.active if slots is not None else None,
            "available_run_slots": slots.available if slots is not None else None,
            "queued_runs": queue["queued"] if queue else 0,
            "running_runs": queue["running"] if queue else 0,
            "interrupted_runs": queue["interrupted"] if queue else 0,
            "inline_worker": settings.inline_worker,
            "serve_ui": settings.serve_ui,
        }

    @app.get("/workflows", tags=["meta"])
    async def list_workflows() -> list[dict[str, Any]]:
        """列出可运行的业务工作流（前端与调度器共用这一份定义）。

        人工构造的验证用图（``is_synthetic``）仍注册在定义集合里，但不在此列出：
        它们的存在意义是给调度与执行的测试当装置，出现在界面的下拉框里只会误导。
        """
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
            if not definition.is_synthetic
        ]

    @app.get("/rules", tags=["meta"])
    async def list_rules() -> dict[str, Any]:
        """当前项目配置的全部规则（运行台「规则清单」页用）。

        **只读且不依赖服务实例**：规则集是进程启动期装载的常量，与运行、数据库都无关。
        因此这里不调 ``get_service()``——服务尚未就绪时也能回答「配了哪些规则」，
        而这正是排查时最先要问的问题。

        返回字段与登记表一一对应，另补三项便于展示的派生值：

        - ``cluster_title``：簇的中文名。前端不该自己维护一份簇名映射，那会在新增簇时漏掉；
        - ``text``：prompt 通道的正文（``rules/texts/*.txt`` 去掉标记后）。它是这条规则
          真正递给模型的话，看规则必须能看到它；
        - ``landings``：代码/正文里的落点行号。规则「登记了但没落地」只能靠它看出来。

        不返回 ``message_customer`` 以外的任何对客文案拼接结果：对客产物不得出现规则编号
        与内部解释，这个边界由产出侧保证，此处只是把原始字段交出去给人看。
        """
        from dataclasses import asdict

        from .reference_standards import is_field_bound
        from .rules import default as default_rules
        from .rules import guard as rule_guard
        from .rules import render as rule_render
        from .rules.clusters import cluster_of

        registry = default_rules()
        blocks = rule_render.load_blocks()
        landings: dict[str, list[dict[str, Any]]] = {}
        for mark in rule_guard.scan_marks():
            landings.setdefault(mark.rule_id, []).append(
                {"path": mark.path, "line": mark.line, "kind": mark.kind}
            )

        rows: list[dict[str, Any]] = []
        for rule in registry.all():
            row = asdict(rule)
            row["cluster_title"] = cluster_of(rule.cluster)
            block = blocks.get(rule.id)
            row["text"] = block.text.strip() if block is not None else ""
            row["landings"] = landings.get(rule.id, [])
            # 逐字段口径已收进参考标准表。**不在这里过滤**：接口仍返回全量，
            # 由页面决定显示哪一页。规则本身照旧生效，过滤只是视图层的事——
            # 若在此处删行，``total`` 与登记表就不再对得上。
            row["in_reference_standards"] = is_field_bound(rule)
            rows.append(row)

        return {
            "total": len(rows),
            "active": len(registry.active()),
            "rules": rows,
        }

    @app.get("/standards", tags=["meta"])
    async def list_standards() -> dict[str, Any]:
        """参考标准表（运行台「参考标准表」页用）。

        与 ``/rules`` 同一定位：只读、不依赖服务实例、进程常量现拼。**没有写接口**——
        这一页只是把散在契约、编外登记表与规则正文里的字段口径汇到一处给人看。

        派生列（名称/业务类型/定义/输出模式）全部现读，不在这里留副本；表里真正
        手写的只有准入模式与三列说明。因此契约一变，刷新页面即对齐。
        """
        from .reference_standards import build_payload

        return build_payload()

    @app.get("/knowledge", tags=["meta"])
    async def get_knowledge() -> dict[str, Any]:
        """当前生效的知识库（运行台「知识库」页用）。

        与 ``/rules``、``/standards`` 同一定位：只读、不依赖服务实例、进程常量现拼。
        知识包是启动期装载的常量，与运行、数据库都无关，因此这里不调 ``get_service()``
        ——服务尚未就绪时也能回答「当前系统认的在售车型是哪几款」。

        **没有写接口，这是定论不是第一版。** 知识内容写入即冻结，要改就改文件重启。

        价格刻意给两段：``core_max_wan`` 是主力价格带上限（对客默认口径），
        ``max_wan`` 是全系包络（含定制/限量版）。只给包络会让人以为 917 卖 110 万。
        """
        from .knowledge.report import build_payload as build_knowledge

        return build_knowledge()

    # ------------------------------------------------------------ 运行

    @app.post("/runs", response_model=SubmitResponse, tags=["runs"])
    async def submit_run(payload: SubmitRequest = Body(...)) -> SubmitResponse:
        """提交任务，立即返回 ``run_id``。

        任务先落库再返回，因此不会出现「拿到 run_id 但
        数据库里查不到」的情况。
        """
        service = get_service()
        inputs = dict(payload.inputs)
        if payload.workflow_id == ENTRY_WORKFLOW_ID and not str(inputs.get("batch_id") or "").strip():
            # 未传或传空白：补缺省批次号。见 DEFAULT_BATCH_ID 的说明——
            # 不补的话运行会在下游节点 KeyError，而调用方拿到的是 200。
            inputs["batch_id"] = DEFAULT_BATCH_ID
        _validate_entry_inputs(payload.workflow_id, inputs)
        try:
            run_id = await service.submit(
                payload.workflow_id,
                inputs,
                business_ref=payload.business_ref,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QueueFull as exc:
            # 队列满时明确拒绝（429），不排队等待。带 Retry-After 让调用方知道该等多久。
            # 触发条件自 V0.5.2 起是**队列深度**而非运行名额——执行已挪到
            # worker，API 侧无从同步判断「运行名额」。
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": "30"},
            ) from exc
        except RunSlotUnavailable as exc:
            # 进程内执行路径（INLINE_WORKER=false 且走本地调度）仍可能报这个。
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": "30"},
            ) from exc

        # 入队已同步落库（Service.submit 内是 await store.enqueue_run），因此这里不必
        # 再等——「先落库再返回 run_id」由入队本身满足。留一次确认只为防实现退化：
        # 若哪天入队改成异步，这一步会立刻失败而不是静默丢掉契约。
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
                definition = dict(version_row.get("definition") or {})

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

    @app.post("/runs/{run_id}/abandon", tags=["runs"])
    async def abandon_run(run_id: str) -> dict[str, Any]:
        """把一条 ``interrupted``（进程中断遗留）的运行显式终结为 ``cancelled``。

        为什么需要这个出口：worker 崩溃或停机时，正在跑的运行会被标成 ``interrupted``，
        并**保留已完成结果、不做自动续跑**——这是对的（进程中断不能证明外部副作用未
        发生，见 既定约束）。但「交人工处理」若没有任何出口，这些
        运行会永远留在列表里，看不出是「待处理」还是「已处理」。本端点给出那个出口。

        **只接受 ``interrupted``**：其它状态一律 409。特别是不能拿它去停一条正在跑的
        运行——那是 ``POST /runs/{run_id}/cancel`` 的职责。两者语义不同：一个是「结掉
        已经停掉的」，一个是「停止正在进行的」。
        """
        service = get_service()
        if service.store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        if await service.store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
        abandoned = await service.store.abandon_run(run_id)
        if not abandoned:
            raise HTTPException(
                status_code=409,
                detail=f"运行 {run_id} 不是 interrupted 状态，无法人工终结",
            )
        return {"run_id": run_id, "abandoned": True}

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
        logger.warning(
            "前端构建产物缺失：{}（SERVE_UI=true）；请先执行 cd frontend && npm run build",
            dist,
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




app = create_app()

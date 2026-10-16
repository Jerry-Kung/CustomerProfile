"""对外任务接口（V0.5.5）：接收前端任务队列推送的单号画像任务。

## 它解决什么

运行台（``customer_profile.api``，8000）是**内部只读面**——提交任务只是它顺带提供的
一个入口，无鉴权、只服务主入口的手机号触发。上线业务测试后，任务由前端任务队列批量
推送，需要一个**有鉴权、只做接单**的对外面。本模块就是那个面。

## 三条口径

1. **异步、不轮询。** 收到即把运行**持久化入队**（``Service.submit``）并返回 200；
   真正执行由 worker 进程完成，回写成功后前端从回写接口侧得知。本进程**不执行**工作流
   ——见第 2 条。
2. **本进程不扮演 worker。** ``inline_worker`` 与 ``interrupt_on_start`` 都被强制关掉：
   - 前者：一个接收进程同时跑 21 分钟的工作流，会把接单能力拖住；而且上线形态本就该是
     「API 只提交、worker 只执行」（见 ``docs/architecture.md`` §2）。
   - 后者：入站进程与运行台、worker **共用同一个 SQLite**。``interrupt_on_start`` 的语义是
     「把上次进程遗留的 running 标为 interrupted」，若在这里执行，会把**别的进程正在跑的**
     运行标成中断——一次无关的接单进程重启就毁掉一次在飞运行。这不是理论风险：
     ``scripts/run_baseline.py`` 为同样的理由显式关掉它（该脚本 docstring 第 3 条）。
3. **密钥独立。** 用 ``INBOUND_API_KEY``，**不复用** ``PROFILE_API_KEY``。后者是我们主动调用
   生产回写服务的**出站**凭据；身兼两用会把「谁能调我们的接口」与「我们能写生产库」
   并成同一个泄露面。未配置时接口整体关闭（503），**不是**关闭校验——空密钥若被当作
   「不校验」，这个端口就是任何人可写的入口，一条外部请求会变成一次真实算力消耗与一次
   生产回写。

节点契约：请求体两键 ``phone_number`` / ``batch_id``；业务流入口写死为业务测试臂
（``ENTRY_WORKFLOW_ID``），与运行台提交页、入参校验同指一个 ID。
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .api import ENTRY_WORKFLOW_ID, PHONE_PATTERN
from .runner import QueueFull, RunSlotUnavailable, Service, build_service
from .settings import Settings, get_settings

TASK_PATH = "/api/v1/tasks"
"""接单端点。前端任务队列按此路径投递。

取 ``/api/v1/`` 前缀与既有外部服务（``/api/v1/remote/...``）保持同一层次感，但不共用
``remote`` 段——那段是**别人给我们的**接口，本路径是**我们给别人的**接口。
"""


def inbound_settings(base: Settings | None = None) -> Settings:
    """构造入站进程的运行配置：沿用环境配置，强制关掉两处会伤到别人的行为。

    见模块头第 2 条。这两项**不通过 ``.env`` 配置**，因此在部署侧无法被误开——
    它们不是可调参数，而是这个进程的定位。
    """
    settings = base or get_settings()
    return settings.model_copy(
        update={"inline_worker": False, "interrupt_on_start": False}
    )


class TaskRequest(BaseModel):
    """一次接单请求。两键，与业务流入口契约同名。"""

    phone_number: str = Field(..., description="11 位中国大陆手机号（1 开头，第二位 3-9）")
    batch_id: str = Field(
        ...,
        description="批次号。必填：它会被原样填进回传体的 batch_id，前端据此判断任务来源",
    )


class TaskAccepted(BaseModel):
    """接单成功（200）。返回 ``run_id`` 供排查；**任务已在返回前落库**。"""

    status: str = "accepted"
    run_id: str
    workflow_id: str
    phone_number: str
    batch_id: str


def create_inbound_app(
    service_factory: Callable[[], Awaitable[Service]] | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    """构造对外任务接口应用。

    与 ``api.create_app`` 的分工：那个按 8000 起、托管运行台、支持查询与人工操作；
    这个只接单。两者不共用同一个 app 对象——共用一个 FastAPI 实例意味着共享同一份
    路由与挂载，而对外写面与内部只读面的暴露范围本就不同。

    ``service_factory`` 可注入（测试给一个用固定响应装配的服务）；缺省在 lifespan 里
    按 ``inbound_settings`` 装配真实服务。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service_factory is not None:
            app.state.service = await service_factory()
        else:
            app.state.service = await build_service(inbound_settings(settings))
        try:
            yield
        finally:
            # 本进程不执行工作流，因此没有「等在飞运行收敛」这一步；只需关掉客户端与存储。
            service = getattr(app.state, "service", None)
            if service is not None:
                await service.aclose()
                if service.store is not None:
                    service.store.close()

    app = FastAPI(
        title="Customer Profile — 对外任务接口",
        version="0.5.5",
        description="接收前端任务队列推送的单号画像任务（异步接单，不做执行与查询）",
        lifespan=lifespan,
    )

    def get_service() -> Service:
        service = getattr(app.state, "service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="服务尚未就绪")
        return service

    def require_key(service: Service, provided: str | None) -> None:
        """校验 ``X-API-Key``。

        三态与各自的理由：

        - **未配置密钥 → 503**：接口关闭。绝不放行（见模块头第 3 条）。
        - **未带请求头 → 401**：没提供凭据，与「提供错了」是两件事，分开才让调用方知道该补什么。
        - **密钥不符 → 403**：与既有回写服务的错误语义一致（``AI画像远程回传接口数据结构说明`` §6）。

        比较走 ``secrets.compare_digest``：普通 ``==`` 在第一个不同字节处短路返回，
        逐字节的耗时差可被用于爆破密钥。
        """
        expected = (service.settings.inbound_api_key or "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="对外任务接口未启用：未配置 INBOUND_API_KEY",
            )
        if not provided:
            raise HTTPException(status_code=401, detail="缺少 X-API-Key 请求头")
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="X-API-Key 无效")

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        """存活探针。**不报告接口是否已启用**——那是未鉴权调用方不该知道的信息。"""
        return {"status": "ok", "interface": "inbound", "workflow_id": ENTRY_WORKFLOW_ID}

    @app.post(TASK_PATH, response_model=TaskAccepted, tags=["tasks"])
    async def accept_task(
        payload: TaskRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> TaskAccepted:
        """接一个单号任务：校验 → 入队 → 立即返回 200。**不执行、不轮询。**

        为什么``phone_number`` 与 ``batch_id`` 都在这里校验，而不是靠 ``start`` 节点：
        少传或传错在 ``start`` 节点只会变成一个**失败的运行**，而调用方拿到的是一条
        「已接收」的 200——错误与结果对不上。手机号还会进 ``/data/history/{phone}``
        与 ``/data/profile/{phone}`` 两条外部路径，非法值在那里表现为难以归因的 404。

        ``batch_id`` 在此**必填**（工作流定义里它是可选的，缺省空串）：回传契约要求它
        与触发时一致，前端据此判断任务来源；空值会让回写体里那一键为空，前端无从归属。
        """
        service = get_service()
        require_key(service, x_api_key)

        phone = (payload.phone_number or "").strip()
        if not PHONE_PATTERN.match(phone):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"phone_number 须为 11 位中国大陆手机号（1 开头，第二位 3-9），"
                    f"实际为 {payload.phone_number!r}"
                ),
            )

        batch_id = (payload.batch_id or "").strip()
        if not batch_id:
            raise HTTPException(
                status_code=422,
                detail="batch_id 必填且不能为空白：回传体要把原值带回，前端据此判断任务来源",
            )

        try:
            run_id = await service.submit(
                ENTRY_WORKFLOW_ID,
                {"phone_number": phone, "batch_id": batch_id},
                # 业务标识取手机号，与运行台提交页同口径：列表可按它检索，子运行也带上同一个值。
                business_ref=phone,
            )
        except KeyError as exc:
            # 入口工作流不可用（定义未通过校验或已摘除）。这不是调用方的错，如实报 500 级问题。
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except QueueFull as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "30"}
            ) from exc
        except RunSlotUnavailable as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "30"}
            ) from exc

        return TaskAccepted(
            run_id=run_id,
            workflow_id=ENTRY_WORKFLOW_ID,
            phone_number=phone,
            batch_id=batch_id,
        )

    return app


app = create_inbound_app()


def main() -> None:
    """``python -m customer_profile.inbound``。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "customer_profile.inbound:app",
        host=settings.inbound_host,
        port=settings.inbound_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

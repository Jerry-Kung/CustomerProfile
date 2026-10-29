"""邀约接单与状态查询；沿用入站进程的鉴权和持久化队列。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import Field

from .invitation_contract import InvitationInputs
from .runner import QueueFull, RunSlotUnavailable, Service
from .workflows.invitation import WORKFLOW_ID, WRITE

INVITATION_TASK_PATH = "/api/v1/invitation-tasks"


class InvitationTaskRequest(InvitationInputs):
    # 新的接单契约必带执行标识；旧系统可在内部 /runs 路径使用兼容输入。
    execution_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


def invitation_run_id(payload: InvitationTaskRequest) -> str:
    identity = json.dumps(
        [payload.phone_number, payload.batch_id, payload.execution_id],
        ensure_ascii=False,
    )
    return "inv_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def register_invitation_routes(
    app: FastAPI, get_service: Callable[[], Service], require_key: Callable
) -> None:
    @app.post(INVITATION_TASK_PATH, tags=["invitation"])
    async def accept_invitation(
        payload: InvitationTaskRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        service = get_service()
        require_key(service, x_api_key)
        try:
            run_id = await service.submit(
                WORKFLOW_ID,
                payload.model_dump(),
                business_ref=payload.phone_number,
                run_id=invitation_run_id(payload),
                idempotent=True,
            )
        except (QueueFull, RunSlotUnavailable) as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "30"}
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=503, detail="邀约工作流不可用") from exc
        return {
            "status": "accepted",
            "run_id": run_id,
            "workflow_id": WORKFLOW_ID,
            **payload.model_dump(),
        }

    @app.get(INVITATION_TASK_PATH + "/{run_id}", tags=["invitation"])
    async def invitation_status(
        run_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")
    ) -> dict[str, Any]:
        service = get_service()
        require_key(service, x_api_key)
        if service.store is None:
            raise HTTPException(status_code=503, detail="运行存储不可用")
        row = await service.store.get_run(run_id)
        if row is None or row["workflow_id"] != WORKFLOW_ID or row.get("parent_run_id"):
            raise HTTPException(status_code=404, detail="邀约运行不存在")
        inputs = row.get("inputs_json") or {}
        outputs = row.get("outputs_json") or {}
        delivery = outputs.get("delivery", "pending")
        if row["status"] in {"failed", "interrupted", "cancelled"}:
            nodes = await service.store.list_node_executions(run_id)
            write = next((node for node in nodes if node["node_id"] == WRITE), None)
            delivery = (
                "unknown"
                if write and write["status"] in {"running", "failed", "succeeded"}
                else "not_attempted"
            )
        return {
            "run_id": run_id,
            "workflow_id": WORKFLOW_ID,
            "status": row["status"],
            "phone_number": inputs.get("phone_number"),
            "batch_id": inputs.get("batch_id"),
            "execution_id": inputs.get("execution_id"),
            "work_env": inputs.get("work_env"),
            "outputs": outputs,
            "delivery": delivery,
            "error": row.get("error"),
        }

"""复用现有留痕，按任务或筛选范围流式导出完整执行过程。"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from .definitions import RunStatus


def register_trace_routes(app: FastAPI, get_service: Callable[[], Any]) -> None:
    @app.get(
        "/traces/export",
        tags=["runs"],
        response_class=StreamingResponse,
        summary="导出完整执行 trace，含全部子运行和模型/ASR 请求记录",
        responses={200: {
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
            "description": "JSONL：每行一个主任务的完整 trace；空范围返回空文件。",
        }},
    )
    async def export_traces(
        run_id: str | None = Query(default=None, min_length=1, description="指定一次运行；可指定子运行"),
        workflow_id: str | None = Query(default=None, min_length=1, description="主任务工作流，如 invitation_strategy"),
        business_ref: str | None = Query(default=None, min_length=1, description="业务标识，如客户手机号"),
        status: str | None = Query(default=None, min_length=1),
        created_after_ms: int | None = Query(default=None, ge=0, le=2**63-1, description="主任务创建时间下界，Unix 毫秒，含边界"),
        created_before_ms: int | None = Query(default=None, ge=0, le=2**63-1, description="主任务创建时间上界，Unix 毫秒，含边界"),
    ) -> StreamingResponse:
        """画像和邀约共用原 API 端口，仅查询留痕，不提交或重跑任务。

        筛选命中的运行 ID 在导出开始时固定。时间/状态条件只筛选主任务；
        每个任务读取一次一致的快照，包含所有深度的子运行、节点输入输出、
        全部请求尝试和当时的定义/提示词哈希/代码提交。运行中的任务反映读取时状态。
        不受 /runs 列表分页上限限制；X-Trace-Count 是本次选中的运行数。
        """
        if (
            created_after_ms is not None and created_before_ms is not None
            and created_after_ms > created_before_ms
        ):
            raise HTTPException(status_code=422, detail="创建时间下界不能晚于上界")
        if status is not None and status not in RunStatus.TERMINAL | {
            RunStatus.QUEUED, RunStatus.RUNNING,
        }:
            raise HTTPException(status_code=422, detail="未知的运行状态")
        store = get_service().store
        if store is None:
            raise HTTPException(status_code=503, detail="未启用留痕存储")
        ids = await store.trace_run_ids(
            run_id=run_id, workflow_id=workflow_id, status=status,
            business_ref=business_ref, created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
        )
        if run_id is not None and not ids:
            raise HTTPException(status_code=404, detail="未找到符合条件的运行")

        async def lines():
            for selected_id in ids:
                trace = await store.execution_trace(selected_id)
                if trace is None:
                    raise RuntimeError(f"导出期间运行 {selected_id} 已不存在，请重新导出")
                yield json.dumps(trace, ensure_ascii=False) + "\n"

        return StreamingResponse(
            lines(), media_type="application/x-ndjson",
            headers={
                "Content-Disposition": 'attachment; filename="execution-traces.jsonl"',
                "X-Trace-Count": str(len(ids)),
            },
        )

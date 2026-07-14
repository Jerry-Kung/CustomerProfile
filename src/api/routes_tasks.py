from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import JSONResponse
from src.api.errors import AppError
from src.api.schemas import (
    DEFAULT_TASK_TYPE,
    SubmitTaskBatchIdForm,
    SubmitTaskPhoneForm,
    SubmitTaskTypeForm,
    SubmitTaskWorkEnvForm,
    TaskAcceptedResponse,
    TaskListItem,
    TaskListQuery,
    TaskListResponse,
    TaskResultFailedResponse,
    TaskResultInProgressResponse,
    TaskResultSucceededResponse,
)
from src.domain.task.errors import TaskQueueFullError
from src.domain.task.service import TaskService

router = APIRouter(prefix="/v1/customer-profile/tasks", tags=["CustomerProfileTasks"])


def _truncate_text(value: str, max_len: int = 4000) -> str:
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}...(truncated)"


async def _build_submit_task_request_log_payload(request: Request) -> dict[str, object]:
    try:
        raw_body = getattr(request.state, "raw_body", None)
        if raw_body is None:
            raw_body = await request.body()
        body_preview = _truncate_text(raw_body.decode("utf-8", errors="replace")) if raw_body else ""
    except Exception as exc:  # pragma: no cover - defensive path
        body_preview = f"<failed_to_read_body:{exc.__class__.__name__}>"

    headers = request.headers
    return {
        "method": request.method,
        "path": request.url.path,
        "query_params": dict(request.query_params),
        "client_host": request.client.host if request.client else None,
        "content_type": headers.get("content-type"),
        "content_length": headers.get("content-length"),
        "user_agent": headers.get("user-agent"),
        "x_request_id": headers.get("x-request-id"),
        "body_preview": body_preview,
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=TaskAcceptedResponse)
async def submit_task(
    request: Request,
    customer_phonenumber: str = Form(..., description="Customer phone; digits and + only"),
    work_env: str | None = Form(None, description="Optional workflow environment: test or production"),
    batch_id: str | None = Form(None, description="Optional client batch id for Dify workflow"),
    task_type: str | None = Form(
        None,
        description="Optional task type: customer_profile (default) or invitation_strategy",
    ),
) -> TaskAcceptedResponse:
    request.app.state.logger.info(
        "submit_task_request_received %s",
        json.dumps(await _build_submit_task_request_log_payload(request), ensure_ascii=True),
    )

    phone = SubmitTaskPhoneForm.model_validate({"customer_phonenumber": customer_phonenumber}).customer_phonenumber
    normalized_work_env = SubmitTaskWorkEnvForm.model_validate({"work_env": work_env}).work_env
    normalized_batch_id = SubmitTaskBatchIdForm.model_validate({"batch_id": batch_id}).batch_id
    normalized_task_type = SubmitTaskTypeForm.model_validate({"task_type": task_type}).task_type

    request.app.state.logger.info(
        "submit_task_request_body customer_phonenumber=%s task_type=%s work_env=%s batch_id=%s",
        phone,
        normalized_task_type,
        normalized_work_env,
        normalized_batch_id,
    )

    service: TaskService = request.app.state.task_service
    try:
        result = service.submit_task(
            phone,
            work_env=normalized_work_env,
            batch_id=normalized_batch_id,
            task_type=normalized_task_type,
        )
    except TaskQueueFullError as exc:
        raise AppError(
            code="TASK_QUEUE_FULL",
            message="task queue is full, please retry later",
            status_code=503,
            details={"max_waiting": exc.max_waiting, "queued_count": exc.queued_count},
        ) from exc
    return TaskAcceptedResponse(
        code="TASK_ACCEPTED",
        message="task accepted",
        customer_phonenumber=result.customer_phonenumber,
        status=result.status,
        task_type=result.task_type,
    )


@router.get("", response_model=TaskListResponse)
def list_tasks(
    request: Request,
    query: TaskListQuery = Depends(),
) -> TaskListResponse:
    service: TaskService = request.app.state.task_service
    items, total = service.list_tasks(
        phone_prefix=query.phone_prefix,
        page=query.page,
        page_size=query.page_size,
        task_type=query.task_type,
    )
    return TaskListResponse(
        code="OK",
        message="success",
        items=[
            TaskListItem(
                customer_phonenumber=item.customer_phonenumber,
                card_id=item.card_id,
                nickname=item.nickname,
                status=item.status,
                updated_at=item.updated_at,
                task_type=item.task_type,
            )
            for item in items
        ],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


@router.get("/{customer_phonenumber}/result")
def get_task_result(
    request: Request,
    customer_phonenumber: str,
    task_type: str | None = None,
):
    if not customer_phonenumber or not all(ch.isdigit() or ch == "+" for ch in customer_phonenumber):
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID_ARGUMENT", "message": "customer_phonenumber format is invalid"},
        )

    try:
        normalized_task_type = SubmitTaskTypeForm.model_validate({"task_type": task_type}).task_type
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID_ARGUMENT", "message": str(exc)},
        )

    service: TaskService = request.app.state.task_service
    item = service.get_task_by_phone(customer_phonenumber, task_type=normalized_task_type)
    if item is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": "TASK_NOT_FOUND",
                "message": "task for this phone number does not exist",
                "task_type": normalized_task_type,
            },
        )
    if item.status in {"queued", "processing"}:
        code = "TASK_QUEUED" if item.status == "queued" else "TASK_PROCESSING"
        payload = TaskResultInProgressResponse(
            code=code,
            message="task is still running",
            customer_phonenumber=item.customer_phonenumber,
            status=item.status,
            updated_at=item.updated_at,
            task_type=item.task_type,
        )
        return JSONResponse(status_code=202, content=payload.model_dump())
    if item.status == "failed":
        error_code = _extract_error_code(item.error_message)
        payload = TaskResultFailedResponse(
            code=error_code,
            message="task failed",
            customer_phonenumber=item.customer_phonenumber,
            status="failed",
            error_message=item.error_message,
            updated_at=item.updated_at,
            task_type=item.task_type,
        )
        return JSONResponse(status_code=422, content=payload.model_dump())

    payload = TaskResultSucceededResponse(
        code="TASK_SUCCEEDED",
        message="task succeeded",
        customer_phonenumber=item.customer_phonenumber,
        status="succeeded",
        output=item.output or {},
        card_id=item.card_id,
        nickname=item.nickname,
        updated_at=item.updated_at,
        task_type=item.task_type,
    )
    return payload


def _extract_error_code(error_message: str | None) -> str:
    known_codes = {
        "DIFY_REQUEST_FAILED",
        "DIFY_TIMEOUT",
        "DIFY_OUTPUT_INVALID_JSON",
        "DIFY_OUTPUT_SCHEMA_MISMATCH",
        "WORKER_UNEXPECTED_ERROR",
    }
    if not error_message:
        return "TASK_FAILED"
    for code in known_codes:
        if error_message.startswith(code):
            return code
    return "TASK_FAILED"

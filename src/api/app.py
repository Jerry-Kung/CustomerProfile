from __future__ import annotations

import os
import json

from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.api.errors import AppError
from src.api.middlewares import request_log_middleware
from src.api.routes_tasks import router as task_router
from src.config.logging_utils import get_logger
from src.domain.task.service import TaskService
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.storage.task_upload_store import TaskUploadStore


def _safe_validation_errors(exc) -> list[dict[str, object]]:
    return [
        {
            "loc": list(item.get("loc", ())),
            "type": item.get("type"),
            "msg": item.get("msg"),
        }
        for item in exc.errors()
    ]


def create_api_app(
    db_client: MySQLClient,
    task_service: TaskService,
    upload_store: TaskUploadStore,
) -> FastAPI:
    app = FastAPI(title="customer_profile backend", version="0.1.0")
    app.state.db_client = db_client
    app.state.task_service = task_service
    app.state.upload_store = upload_store
    app.state.logger = get_logger("api")
    raw_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
    allow_origins = [s.strip() for s in raw_origins.split(",") if s.strip()]
    if not allow_origins:
        allow_origins = ["*"]
    allow_all = "*" in allow_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else allow_origins,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.middleware("http")
    async def _request_middleware(request: Request, call_next):
        return await request_log_middleware(request, call_next)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        app.state.logger.warning(
            "http_application_error method=%s path=%s status=%s error_code=%s detail_keys=%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            sorted(exc.details.keys()) if isinstance(exc.details, dict) else None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        if request.method == "POST" and request.url.path == "/v1/customer-profile/tasks":
            request_headers = {
                "content_type": request.headers.get("content-type"),
                "user_agent": request.headers.get("user-agent"),
                "origin": request.headers.get("origin"),
                "referer": request.headers.get("referer"),
                "x_request_id": request.headers.get("x-request-id"),
            }
            app.state.logger.warning(
                "submit_task_invalid_request method=%s path=%s query_keys=%s headers=%s body_size_bytes=%s validation_errors=%s",
                request.method,
                request.url.path,
                sorted(request.query_params.keys()),
                json.dumps(request_headers, ensure_ascii=True),
                len(getattr(request.state, "raw_body", None) or b""),
                json.dumps(_safe_validation_errors(exc), ensure_ascii=True),
            )
        else:
            app.state.logger.warning(
                "http_request_validation_failed method=%s path=%s errors=%s",
                request.method,
                request.url.path,
                json.dumps(_safe_validation_errors(exc), ensure_ascii=True),
            )
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_ARGUMENT",
                "message": "invalid request",
                "details": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_validation_error_handler(request: Request, exc: ValidationError):
        app.state.logger.warning(
            "pydantic_validation_failed method=%s path=%s errors=%s",
            request.method,
            request.url.path,
            json.dumps(_safe_validation_errors(exc), ensure_ascii=True),
        )
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_ARGUMENT",
                "message": "invalid request",
                "details": jsonable_encoder(exc.errors()),
            },
        )

    @app.get("/health")
    def health_check() -> dict[str, str]:
        db_ok = app.state.db_client.ping()
        return {"status": "ok", "db_status": "up" if db_ok else "down"}

    app.include_router(task_router)

    return app


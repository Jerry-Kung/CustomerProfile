from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from fastapi import Request, Response

from src.config.observability import elapsed_ms, log_context, safe_text


async def request_log_middleware(
    request: Request, call_next: Callable[[Request], Response]
) -> Response:
    start = time.perf_counter()
    incoming_request_id = request.headers.get("x-request-id", "").strip()
    request_id = safe_text(incoming_request_id, 128) if incoming_request_id else uuid.uuid4().hex
    request.state.request_id = request_id
    try:
        # Read and cache raw body early so downstream handlers can still log it
        # after FastAPI consumes form/json payloads.
        request.state.raw_body = await request.body()
    except Exception:  # pragma: no cover - defensive path
        request.state.raw_body = None
    with log_context(request_id=request_id):
        request.app.state.logger.info(
            "http_request_started method=%s path=%s query_keys=%s client_host=%s content_type=%s content_length=%s",
            request.method,
            request.url.path,
            sorted(request.query_params.keys()),
            request.client.host if request.client else None,
            request.headers.get("content-type"),
            request.headers.get("content-length"),
        )
        try:
            response = await call_next(request)
        except Exception:
            request.app.state.logger.exception(
                "http_request_unhandled_error method=%s path=%s status=500 elapsed_ms=%s",
                request.method,
                request.url.path,
                elapsed_ms(start),
            )
            raise
        response.headers["X-Request-ID"] = request_id
        request.app.state.logger.info(
            "http_request_completed method=%s path=%s status=%s elapsed_ms=%s response_content_length=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms(start),
            response.headers.get("content-length"),
        )
        return response

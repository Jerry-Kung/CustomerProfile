from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Request, Response


async def request_log_middleware(
    request: Request, call_next: Callable[[Request], Response]
) -> Response:
    start = time.perf_counter()
    try:
        # Read and cache raw body early so downstream handlers can still log it
        # after FastAPI consumes form/json payloads.
        request.state.raw_body = await request.body()
    except Exception:  # pragma: no cover - defensive path
        request.state.raw_body = None
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    request.app.state.logger.info(
        "http_request method=%s path=%s status=%s elapsed_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


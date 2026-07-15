from __future__ import annotations

import contextvars
import re
import time
from contextlib import contextmanager
from typing import Iterator


_LOG_FIELDS = (
    "request_id",
    "task_id",
    "batch_id",
    "task_type",
    "work_env",
    "workflow_platform",
)

_context: dict[str, contextvars.ContextVar[str]] = {
    name: contextvars.ContextVar(f"log_{name}", default="-") for name in _LOG_FIELDS
}


class LogContextFilter:
    """Populate stable correlation fields expected by the shared log formatter."""

    def filter(self, record) -> bool:
        for name, variable in _context.items():
            setattr(record, name, variable.get())
        return True


@contextmanager
def log_context(**values: object) -> Iterator[None]:
    tokens: list[tuple[contextvars.ContextVar[str], contextvars.Token[str]]] = []
    try:
        for name, value in values.items():
            variable = _context.get(name)
            if variable is not None and value not in (None, ""):
                tokens.append((variable, variable.set(str(value))))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def current_log_context() -> dict[str, str]:
    """Return the correlation fields for the current thread or async task."""
    return {name: variable.get() for name, variable in _context.items()}


def mask_phone(value: str | None) -> str:
    """Retain enough shape for support diagnostics without logging the full phone number."""
    if not value:
        return "-"
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    if len(text) <= 2:
        return "*" * len(text)
    return f"{text[0]}***{text[-1]}"


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def safe_text(value: object, max_length: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= max_length else f"{text[:max_length]}...(truncated)"

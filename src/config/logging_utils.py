from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.config.settings import AppConfig


def _mask_secret(value: str) -> str:
    if not value:
        return "***"
    prefix = value[:4] if len(value) >= 4 else value[0]
    return f"{prefix}*** (len={len(value)})"


def redact_config(config: AppConfig) -> dict[str, Any]:
    return {
        "app_env": config.app_env,
        "log_level": config.log_level,
        "workflow_platform": config.workflow_platform,
        "dify": {
            "base_url": config.dify.base_url,
            "run_url": config.dify.run_url,
            "api_key_test": _mask_secret(config.dify.api_key_test),
            "api_key_production": _mask_secret(config.dify.api_key_production),
            "invitation_strategy_api_key": _mask_secret(config.dify.invitation_strategy_api_key),
            "user_id": config.dify.user_id,
            "image_mode": config.dify.image_mode,
            "stream_init_timeout_ms": config.dify.stream_init_timeout_ms,
            "poll_interval_ms": config.dify.poll_interval_ms,
            "poll_timeout_ms": config.dify.poll_timeout_ms,
        },
        "qingtian": {
            "base_url": config.qingtian.base_url,
            "api_key_test": _mask_secret(config.qingtian.api_key_test),
            "api_key_production": _mask_secret(config.qingtian.api_key_production),
            "invitation_strategy_api_key": _mask_secret(config.qingtian.invitation_strategy_api_key),
            "user_id": config.qingtian.user_id,
            "poll_interval_ms": config.qingtian.poll_interval_ms,
            "poll_timeout_ms": config.qingtian.poll_timeout_ms,
        },
        "mysql": {
            "host": config.mysql.host,
            "port": config.mysql.port,
            "user": config.mysql.user,
            "password": _mask_secret(config.mysql.password),
            "database": config.mysql.database,
        },
        "upload": {
            "base_dir": str(config.upload.base_dir),
            "max_bytes_per_file": config.upload.max_bytes_per_file,
        },
        "worker": {
            "concurrency": config.worker.concurrency,
            "poll_interval_seconds": config.worker.poll_interval_seconds,
        },
    }


def get_logger(name: str = "customer_profile") -> logging.Logger:
    """Return a named logger.

    Output is delivered by the root handler installed by `configure_root_logging()`,
    so logs from modules that hold their own `logging.getLogger(__name__)` (e.g. the
    Dify client) reach the same stream as logs from named-bootstrap callers.

    Callers that import this module without going through `bootstrap()` (mainly
    tests) still see records via pytest's `caplog`/root-level capture; their stderr
    just stays quiet unless they configure logging themselves.
    """
    logger = logging.getLogger(name)
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    return logger


_NOISY_LIBRARY_LOGGERS = ("httpx", "httpcore")


def configure_root_logging() -> None:
    """Attach a stream handler + level to the root logger.

    Modules that hold their own `logging.getLogger(__name__)` (`src.infrastructure.dify.client`,
    `output_parser`, `tos.client`) never get touched by `get_logger()`, so without this their
    INFO/WARNING records propagate up to a handler-less root and silently disappear. That made
    failures inside the Dify call invisible — the worker logged `worker_dify_invoke_begin` and
    then nothing, even when an exception was being raised.

    Idempotent: re-running just refreshes the level and skips handler duplication.
    """
    root = logging.getLogger()
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    root.setLevel(getattr(logging, level_name, logging.INFO))
    # Cap httpx/httpcore — they emit one INFO per request, which floods the log when the
    # Worker polls QingTian/Dify every 2s. Override with HTTP_LOG_LEVEL=DEBUG when needed.
    http_level_name = os.getenv("HTTP_LOG_LEVEL", "WARNING").strip().upper() or "WARNING"
    http_level = getattr(logging, http_level_name, logging.WARNING)
    for noisy in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(noisy).setLevel(http_level)
    for existing in root.handlers:
        if getattr(existing, "_customer_profile_root", False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler._customer_profile_root = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def log_config_summary(logger: logging.Logger, config: AppConfig) -> None:
    logger.info("config_loaded %s", json.dumps(redact_config(config), ensure_ascii=True))

from __future__ import annotations

import json
import inspect
import logging
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from loguru import logger as loguru_logger

from src.config.settings import AppConfig, env_value
from src.config.observability import current_log_context


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
        "runner": {
            "base_url_test": config.runner.base_url_test,
            "base_url_production": config.runner.base_url_production,
            "request_timeout_ms": config.runner.request_timeout_ms,
            "poll_interval_ms": config.runner.poll_interval_ms,
            "poll_timeout_ms": config.runner.poll_timeout_ms,
        },
        "mysql": {
            "host": config.mysql.host,
            "port": config.mysql.port,
            "user": config.mysql.user,
            "password": _mask_secret(config.mysql.password),
            "database": config.mysql.database,
        },
        "worker": {
            "concurrency": config.worker.concurrency,
            "poll_interval_seconds": config.worker.poll_interval_seconds,
            "role": config.worker.role,
            "target_id": config.worker.target_id,
            "target_revision": config.worker.target_revision,
            "target_mode": config.worker.target_mode,
            "task_types": list(config.worker.task_types),
            "work_envs": list(config.worker.work_envs),
            "target_failure_threshold": config.worker.target_failure_threshold,
            "target_cooldown_seconds": config.worker.target_cooldown_seconds,
        },
        "retry": {
            "policy_version": config.retry.policy_version,
            "max_workflow_attempts": config.retry.max_workflow_attempts,
            "max_poll_recoveries": config.retry.max_poll_recoveries,
            "max_run_lifetime_seconds": config.retry.max_run_lifetime_seconds,
            "backoff_seconds": list(config.retry.backoff_seconds),
            "lease_seconds": config.retry.lease_seconds,
            "lease_heartbeat_seconds": config.retry.lease_heartbeat_seconds,
            "recovery_interval_seconds": config.retry.recovery_interval_seconds,
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
    level_name = env_value("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    return logger


_NOISY_LIBRARY_LOGGERS = ("httpx", "httpcore")
_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
    "{name}:{function}:{line} | component={extra[component]} "
    "request_id={extra[request_id]} task_id={extra[task_id]} "
    "batch_id={extra[batch_id]} task_type={extra[task_type]} "
    "work_env={extra[work_env]} workflow_platform={extra[workflow_platform]} "
    "execution_pool={extra[execution_pool]} workflow_target_id={extra[workflow_target_id]} "
    "retry_policy_version={extra[retry_policy_version]} - {message}"
)
_configured_component: str | None = None


def _level_name(value: str, fallback: str) -> str:
    candidate = value.strip().upper() or fallback
    try:
        loguru_logger.level(candidate)
    except ValueError:
        return fallback
    return candidate


class _InterceptHandler(logging.Handler):
    """Route standard-library records through the configured Loguru sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = inspect.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        loguru_logger.opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
        )


def _patch_loguru_record(record: dict[str, Any]) -> None:
    record["extra"].update(current_log_context())


def _instance_name() -> str:
    # Compose replicas share the log volume, but must rotate separate files.
    return env_value("POD_NAME") or env_value("HOSTNAME") or platform.node() or "local"


def configure_root_logging(component: str = "app") -> None:
    """Configure Loguru sinks and route all standard logging through them.

    Modules that hold their own `logging.getLogger(__name__)` (`src.infrastructure.dify.client`,
    `output_parser`, `tos.client`) never get touched by `get_logger()`, so without this their
    INFO/WARNING records propagate up to a handler-less root and silently disappear. That made
    failures inside the Dify call invisible — the worker logged `worker_dify_invoke_begin` and
    then nothing, even when an exception was being raised.

    The application keeps using ``logging.Logger`` at call sites for compatibility,
    while Loguru exclusively owns formatting, stderr output, file rotation and retention.
    """
    global _configured_component

    root = logging.getLogger()
    level_name = _level_name(env_value("LOG_LEVEL", "INFO"), "INFO")
    root.setLevel(getattr(logging, level_name, logging.INFO))
    # Cap httpx/httpcore — they emit one INFO per request, which floods the log when the
    # Worker polls QingTian/Dify every 2s. Override with HTTP_LOG_LEVEL=DEBUG when needed.
    http_level_name = _level_name(env_value("HTTP_LOG_LEVEL", "WARNING"), "WARNING")
    http_level = getattr(logging, http_level_name, logging.WARNING)
    for noisy in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(noisy).setLevel(http_level)

    if _configured_component == component:
        return

    log_dir = Path(env_value("LOG_DIR", "logs").strip() or "logs").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    pod_name = _instance_name()
    file_pattern = log_dir / f"customer-profile-{component}_{pod_name}_{{time:YYYY-MM-DD}}.log"

    loguru_logger.remove()
    loguru_logger.configure(
        extra={"component": component, **current_log_context()},
        patcher=_patch_loguru_record,
    )
    loguru_logger.add(
        sys.stderr,
        level=level_name,
        format=_LOG_FORMAT,
        diagnose=False,
    )
    loguru_logger.add(
        str(file_pattern),
        level=level_name,
        format=_LOG_FORMAT,
        rotation=env_value("LOG_ROTATION", "00:00"),
        retention=env_value("LOG_RETENTION", "30 days"),
        encoding="utf-8",
        enqueue=True,
        diagnose=False,
    )

    root.handlers = [_InterceptHandler()]
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    _configured_component = component


def log_config_summary(logger: logging.Logger, config: AppConfig) -> None:
    logger.info("config_loaded %s", json.dumps(redact_config(config), ensure_ascii=True))


def log_runtime_identity(logger: logging.Logger, *, component: str, app_env: str) -> None:
    try:
        package_version = version("customer-profile-backend")
    except PackageNotFoundError:
        package_version = "unknown"
    logger.info(
        "component_ready component=%s version=%s git_commit=%s app_env=%s pod_name=%s namespace=%s node_name=%s python_version=%s",
        component,
        package_version,
        env_value("GIT_COMMIT", env_value("IMAGE_TAG", "unknown")),
        app_env,
        _instance_name(),
        env_value("POD_NAMESPACE", "unknown"),
        env_value("NODE_NAME", "unknown"),
        platform.python_version(),
    )

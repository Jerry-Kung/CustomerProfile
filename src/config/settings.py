from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

_DOTENV_LOADED = False


def _load_dotenv_from_project_root() -> None:
    """Load `.env` from repo root so local runs pick up vars without exporting them."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {key}")
    return value


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid integer value for {key}: {raw}") from exc


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class DifyConfig:
    base_url: str
    run_url: str
    api_key_test: str
    api_key_production: str
    invitation_strategy_api_key: str
    user_id: str
    image_mode: str
    stream_init_timeout_ms: int
    poll_interval_ms: int
    poll_timeout_ms: int


@dataclass(frozen=True)
class QingTianConfig:
    """QingTian (DFMC self-built Agent platform) — same async submit + poll shape as Dify."""

    base_url: str
    api_key_test: str
    api_key_production: str
    invitation_strategy_api_key: str
    user_id: str
    poll_interval_ms: int
    poll_timeout_ms: int


@dataclass(frozen=True)
class ProfileRunnerConfig:
    base_url_test: str = ""
    base_url_production: str = ""
    request_timeout_ms: int = 15000
    poll_interval_ms: int = 2000
    poll_timeout_ms: int = 2400000


@dataclass(frozen=True)
class InvitationRunnerConfig:
    base_url_test: str = ""
    base_url_production: str = ""
    api_key_test: str = field(default="", repr=False)
    api_key_production: str = field(default="", repr=False)
    request_timeout_ms: int = 15000
    poll_interval_ms: int = 2000
    poll_timeout_ms: int = 2400000


@dataclass(frozen=True)
class FailureCallbackConfig:
    enabled: bool = False
    base_url_test: str = ""
    base_url_production: str = ""
    api_key_test: str = ""
    api_key_production: str = ""
    timeout_seconds: int = 10


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class UploadConfig:
    """Directory for multipart task images; must be readable by the worker when using file_id mode."""

    base_dir: Path
    max_bytes_per_file: int


@dataclass(frozen=True)
class WorkerConfig:
    """Per-process execution capacity and capability scope; DB claims use SKIP LOCKED."""

    concurrency: int
    poll_interval_seconds: float
    role: str = "local"
    target_id: str = "local-default"
    target_revision: str = "v1"
    target_mode: str = "active"
    task_types: tuple[str, ...] = ("customer_profile", "invitation_strategy")
    work_envs: tuple[str, ...] = ("test", "production")
    target_failure_threshold: int = 3
    target_cooldown_seconds: int = 60


@dataclass(frozen=True)
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 18008
    cors_allow_origins: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class RetryConfig:
    """Repository-owned retry controls for recoverable QingTian workflow runs.

    This is the default QingTian execution path: a returned ``runId`` is
    persisted and can be polled again after a process or network failure.
    No workflow-side change is required.
    """

    max_poll_recoveries: int = 3
    max_run_lifetime_seconds: int = 7200
    max_workflow_attempts: int = 3
    policy_version: str = "single_target_v1"
    backoff_seconds: tuple[int, ...] = (30, 120, 600)
    lease_seconds: int = 180
    lease_heartbeat_seconds: int = 30
    recovery_interval_seconds: int = 30


@dataclass(frozen=True)
class TosConfig:
    """ByteDance Volcano Engine TOS (Object Storage Service) configuration."""

    access_key_id: str
    access_key_secret: str
    region: str
    endpoint: str
    bucket: str
    presigned_url_expires_seconds: int


@dataclass(frozen=True)
class MigrationConfig:
    """Schema migration runner: how long to wait for MySQL on startup."""

    db_wait_max_tries: int
    db_wait_interval_seconds: float


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    log_level: str
    workflow_platform: str
    dify: DifyConfig
    qingtian: QingTianConfig
    mysql: MySQLConfig
    worker: WorkerConfig
    migration: MigrationConfig
    task_queue_max_waiting: int = 500
    retry: RetryConfig = field(default_factory=RetryConfig)
    runner: ProfileRunnerConfig = field(default_factory=ProfileRunnerConfig)
    failure_callback: FailureCallbackConfig = field(default_factory=FailureCallbackConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    invitation_runner: InvitationRunnerConfig = field(default_factory=InvitationRunnerConfig)


_SUPPORTED_PLATFORMS = ("dify", "qingtian", "runner", "invitation_runner")


def load_mysql_config() -> MySQLConfig:
    """Load database settings for tools that do not invoke a workflow."""
    _load_dotenv_from_project_root()
    return MySQLConfig(
        host=_require_env("MYSQL_HOST"),
        port=_env_int("MYSQL_PORT", 3306),
        user=_require_env("MYSQL_USER"),
        password=_require_env("MYSQL_PASSWORD"),
        database=_require_env("MYSQL_DATABASE"),
    )


def env_value(key: str, default: str = "") -> str:
    """Read runtime/logging options through the same dotenv precedence rules."""
    _load_dotenv_from_project_root()
    return _env_str(key, default)


def load_config(*, component: str = "worker") -> AppConfig:
    """Only validate settings needed by the process being started.

    API and migrations must remain available when a workflow is unconfigured.
    Workers share one configuration contract regardless of platform or cost pool.
    """
    if component not in {"api", "worker", "migration"}:
        raise ValueError("component must be api, worker, or migration")
    _load_dotenv_from_project_root()
    is_worker = component == "worker"
    platform = _env_str("WORKFLOW_PLATFORM", "qingtian").lower()
    if is_worker and platform not in _SUPPORTED_PLATFORMS:
        raise ValueError("WORKFLOW_PLATFORM must be qingtian, runner or invitation_runner (dify is legacy compatibility)")
    role = _worker_role() if is_worker else "local"
    task_types = _worker_task_types(workflow_platform=platform) if is_worker else WorkerConfig.task_types
    work_envs = _worker_work_envs(workflow_platform=platform) if is_worker else WorkerConfig.work_envs
    target_id = _worker_target_id(role) if is_worker else "local-default"
    if is_worker and platform in {"runner", "invitation_runner"}:
        target_id = _require_env("WORKFLOW_TARGET_ID")
        if target_id in {"local-default", "commercial-default"}:
            raise ValueError(f"{platform} requires an independent WORKFLOW_TARGET_ID")
    origins = tuple(v.strip() for v in _env_str("CORS_ALLOW_ORIGINS", "*").split(",") if v.strip())
    api_port = _positive_env_int("API_PORT", 18008) if component == "api" else 18008
    if api_port > 65535:
        raise ValueError("API_PORT must be <= 65535")
    failure_callback = _failure_callback_config() if is_worker else FailureCallbackConfig()
    if is_worker and platform == "invitation_runner" and not failure_callback.enabled:
        raise ValueError("invitation_runner requires PROFILE_FAILURE_CALLBACK_ENABLED=true for delivery verification")
    return AppConfig(
        app_env=_env_str("APP_ENV", "local") or "local",
        log_level=_env_str("LOG_LEVEL", "INFO") or "INFO",
        workflow_platform=platform,
        mysql=load_mysql_config(),
        dify=_dify_config(selected=is_worker and platform == "dify", task_types=task_types, work_envs=work_envs),
        qingtian=_qingtian_config(selected=is_worker and platform == "qingtian", task_types=task_types, work_envs=work_envs),
        runner=(_runner_config(workflow_platform=platform, work_envs=work_envs)
                if is_worker and platform == "runner" else ProfileRunnerConfig()),
        failure_callback=failure_callback,
        invitation_runner=(_invitation_runner_config(work_envs=work_envs)
                           if is_worker and platform == "invitation_runner" else InvitationRunnerConfig()),
        worker=WorkerConfig(
            concurrency=_worker_concurrency() if is_worker else 1,
            poll_interval_seconds=_positive_env_int("WORKER_POLL_INTERVAL_MS", 2000) / 1000 if is_worker else 2,
            role=role,
            target_id=target_id,
            target_revision=_worker_target_revision() if is_worker else "v1",
            target_mode=_worker_target_mode() if is_worker else "active",
            task_types=task_types,
            work_envs=work_envs,
            target_failure_threshold=_worker_target_failure_threshold() if is_worker else 3,
            target_cooldown_seconds=_worker_target_cooldown_seconds() if is_worker else 60,
        ),
        migration=MigrationConfig(
            db_wait_max_tries=_migration_db_wait_max_tries(),
            db_wait_interval_seconds=_positive_env_int("MIGRATION_DB_WAIT_INTERVAL_MS", 2000) / 1000,
        ),
        task_queue_max_waiting=_task_queue_max_waiting() if component != "migration" else 500,
        retry=_retry_config() if component != "migration" else RetryConfig(),
        api=ApiConfig(host=_env_str("API_HOST", "0.0.0.0") or "0.0.0.0", port=api_port,
                      cors_allow_origins=origins or ("*",)),
    )


def _qingtian_config(*, selected: bool, task_types: tuple[str, ...], work_envs: tuple[str, ...]) -> QingTianConfig:
    if not selected:
        return QingTianConfig("", "", "", "", "", 2000, 2400000)
    profile = "customer_profile" in task_types
    return QingTianConfig(
        base_url=_validated_url("QINGTIAN_BASE_URL", required=True),
        api_key_test=_require_or_empty("QINGTIAN_API_KEY_TEST", profile and "test" in work_envs),
        api_key_production=_require_or_empty("QINGTIAN_API_KEY_PRODUCTION", profile and "production" in work_envs),
        invitation_strategy_api_key=_require_or_empty("QINGTIAN_INVITATION_STRATEGY_API_KEY", "invitation_strategy" in task_types),
        user_id=_require_env("QINGTIAN_USER_ID"),
        poll_interval_ms=_positive_env_int("QINGTIAN_POLL_INTERVAL_MS", 2000),
        poll_timeout_ms=_positive_env_int("QINGTIAN_POLL_TIMEOUT_MS", 2400000),
    )


def _dify_config(*, selected: bool, task_types: tuple[str, ...], work_envs: tuple[str, ...]) -> DifyConfig:
    if not selected:
        return DifyConfig("", "", "", "", "", "", "file_id", 30000, 2000, 2400000)
    profile = "customer_profile" in task_types
    test_key = _env_str("DIFY_API_KEY_TEST") or _env_str("DIFY_API_KEY")
    if profile and "test" in work_envs and not test_key:
        raise ValueError("missing required environment variable: DIFY_API_KEY_TEST")
    return DifyConfig(
        base_url=_validated_url("DIFY_BASE_URL", required=True),
        run_url=_validated_url("DIFY_RUN_URL", required=True),
        api_key_test=test_key,
        api_key_production=_require_or_empty("DIFY_API_KEY_PRODUCTION", profile and "production" in work_envs),
        invitation_strategy_api_key=_require_or_empty("DIFY_INVITATION_STRATEGY_API_KEY", "invitation_strategy" in task_types),
        user_id=_require_env("DIFY_USER_ID"),
        image_mode="file_id",
        stream_init_timeout_ms=_positive_env_int("DIFY_STREAM_INIT_TIMEOUT_MS", 30000),
        poll_interval_ms=_positive_env_int("DIFY_POLL_INTERVAL_MS", 2000),
        poll_timeout_ms=_positive_env_int("DIFY_POLL_TIMEOUT_MS", 2400000),
    )


def _require_or_empty(key: str, required: bool) -> str:
    if required:
        return _require_env(key)
    return _env_str(key, "")


def _worker_concurrency() -> int:
    n = _env_int("WORKER_CONCURRENCY", 1)
    if n < 1:
        raise ValueError("WORKER_CONCURRENCY must be >= 1")
    return n


def _worker_role() -> str:
    value = _env_str("WORKER_ROLE", "local").lower()
    if value not in {"local", "commercial"}:
        raise ValueError("WORKER_ROLE must be one of: local, commercial")
    return value


_TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TARGET_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _worker_target_id(worker_role: str) -> str:
    value = _env_str("WORKFLOW_TARGET_ID", f"{worker_role}-default")
    if not _TARGET_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "WORKFLOW_TARGET_ID must be 1-128 characters using letters, digits, dot, underscore, or dash"
        )
    return value


def _worker_target_revision() -> str:
    value = _env_str("WORKFLOW_TARGET_REVISION", "v1")
    if not _TARGET_REVISION_PATTERN.fullmatch(value):
        raise ValueError(
            "WORKFLOW_TARGET_REVISION must be 1-64 characters using letters, digits, dot, underscore, or dash"
        )
    return value


def _worker_target_mode() -> str:
    value = _env_str("WORKER_TARGET_MODE", "active").lower()
    if value not in {"active", "draining", "recovery_only"}:
        raise ValueError("WORKER_TARGET_MODE must be one of: active, draining, recovery_only")
    return value


def _csv_values(key: str, default: str) -> tuple[str, ...]:
    values = tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in _env_str(key, default).split(",")
            if item.strip()
        )
    )
    if not values:
        raise ValueError(f"{key} must contain at least one value")
    return values


def _worker_task_types(*, workflow_platform: str = "dify") -> tuple[str, ...]:
    default = {"runner": "customer_profile", "invitation_runner": "invitation_strategy"}.get(
        workflow_platform, "customer_profile,invitation_strategy")
    values = _csv_values("WORKER_TASK_TYPES", default)
    unsupported = sorted(set(values) - {"customer_profile", "invitation_strategy"})
    if unsupported:
        raise ValueError(f"WORKER_TASK_TYPES contains unsupported values: {', '.join(unsupported)}")
    if workflow_platform == "runner" and "invitation_strategy" in values:
        raise ValueError("runner does not support WORKER_TASK_TYPES=invitation_strategy")
    if workflow_platform == "invitation_runner" and values != ("invitation_strategy",):
        raise ValueError("invitation_runner requires WORKER_TASK_TYPES=invitation_strategy")
    return values


def _worker_work_envs(*, workflow_platform: str = "dify") -> tuple[str, ...]:
    values = _csv_values("WORKER_WORK_ENVS", "test" if workflow_platform in {"runner", "invitation_runner"} else "test,production")
    unsupported = sorted(set(values) - {"test", "production"})
    if unsupported:
        raise ValueError(f"WORKER_WORK_ENVS contains unsupported values: {', '.join(unsupported)}")
    return values


def _worker_target_failure_threshold() -> int:
    value = _env_int("WORKER_TARGET_FAILURE_THRESHOLD", 3)
    if value < 1:
        raise ValueError("WORKER_TARGET_FAILURE_THRESHOLD must be >= 1")
    return value


def _worker_target_cooldown_seconds() -> int:
    value = _env_int("WORKER_TARGET_COOLDOWN_SECONDS", 60)
    if value < 1:
        raise ValueError("WORKER_TARGET_COOLDOWN_SECONDS must be >= 1")
    return value


def _task_queue_max_waiting() -> int:
    n = _env_int("TASK_QUEUE_MAX_WAITING", 500)
    if n < 1:
        raise ValueError("TASK_QUEUE_MAX_WAITING must be >= 1")
    return n


def _retry_config() -> RetryConfig:
    max_poll_recoveries = _env_int("RETRY_MAX_POLL_RECOVERIES", 3)
    max_run_lifetime_seconds = _env_int("RETRY_MAX_RUN_LIFETIME_SECONDS", 7200)
    max_workflow_attempts = _env_int("RETRY_MAX_WORKFLOW_ATTEMPTS", 3)
    policy_version = _env_str("RETRY_POLICY_VERSION", "single_target_v1").lower()
    if max_poll_recoveries < 1:
        raise ValueError("RETRY_MAX_POLL_RECOVERIES must be >= 1")
    if max_run_lifetime_seconds < 1:
        raise ValueError("RETRY_MAX_RUN_LIFETIME_SECONDS must be >= 1")
    if max_workflow_attempts < 1:
        raise ValueError("RETRY_MAX_WORKFLOW_ATTEMPTS must be >= 1")
    if policy_version not in {
        "throughput_first_v1",
        "local_first_v1",
        "single_target_v1",
    }:
        raise ValueError(
            "RETRY_POLICY_VERSION must be one of: "
            "throughput_first_v1, local_first_v1, single_target_v1"
        )
    return RetryConfig(
        max_poll_recoveries=max_poll_recoveries,
        max_run_lifetime_seconds=max_run_lifetime_seconds,
        max_workflow_attempts=max_workflow_attempts,
        policy_version=policy_version,
    )


def _migration_db_wait_max_tries() -> int:
    n = _env_int("MIGRATION_DB_WAIT_MAX_TRIES", 30)
    if n < 1:
        raise ValueError("MIGRATION_DB_WAIT_MAX_TRIES must be >= 1")
    return n


def _validated_url(key: str, *, required: bool) -> str:
    value = (_require_env(key) if required else _env_str(key)).rstrip("/")
    if value:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError(f"{key} must be an HTTP(S) base URL without credentials, query or fragment")
    return value


def _positive_env_int(key: str, default: int) -> int:
    value = _env_int(key, default)
    if value < 1:
        raise ValueError(f"{key} must be >= 1")
    return value


def _runner_config(*, workflow_platform: str, work_envs: tuple[str, ...]) -> ProfileRunnerConfig:
    return ProfileRunnerConfig(
        base_url_test=_validated_url("PROFILE_RUNNER_BASE_URL_TEST", required=workflow_platform == "runner" and "test" in work_envs),
        base_url_production=_validated_url("PROFILE_RUNNER_BASE_URL_PRODUCTION", required=workflow_platform == "runner" and "production" in work_envs),
        request_timeout_ms=_positive_env_int("PROFILE_RUNNER_REQUEST_TIMEOUT_MS", 15000),
        poll_interval_ms=_positive_env_int("PROFILE_RUNNER_POLL_INTERVAL_MS", 2000),
        poll_timeout_ms=_positive_env_int("PROFILE_RUNNER_POLL_TIMEOUT_MS", 2400000),
    )


def _invitation_runner_config(*, work_envs: tuple[str, ...]) -> InvitationRunnerConfig:
    return InvitationRunnerConfig(
        base_url_test=_validated_url("INVITATION_RUNNER_BASE_URL_TEST", required="test" in work_envs),
        base_url_production=_validated_url("INVITATION_RUNNER_BASE_URL_PRODUCTION", required="production" in work_envs),
        api_key_test=_require_or_empty("INVITATION_RUNNER_API_KEY_TEST", "test" in work_envs),
        api_key_production=_require_or_empty("INVITATION_RUNNER_API_KEY_PRODUCTION", "production" in work_envs),
        request_timeout_ms=_positive_env_int("INVITATION_RUNNER_REQUEST_TIMEOUT_MS", 15000),
        poll_interval_ms=_positive_env_int("INVITATION_RUNNER_POLL_INTERVAL_MS", 2000),
        poll_timeout_ms=_positive_env_int("INVITATION_RUNNER_POLL_TIMEOUT_MS", 2400000),
    )


def _failure_callback_config() -> FailureCallbackConfig:
    raw_enabled = _env_str("PROFILE_FAILURE_CALLBACK_ENABLED", "false").lower()
    if raw_enabled not in {"true", "false", "1", "0"}:
        raise ValueError("PROFILE_FAILURE_CALLBACK_ENABLED must be true/false")
    enabled = raw_enabled in {"true", "1"}
    base_url = _validated_url("PROFILE_API_BASE_URL", required=enabled)
    api_key = _require_env("PROFILE_API_KEY") if enabled else _env_str("PROFILE_API_KEY")
    return FailureCallbackConfig(
        enabled=enabled,
        base_url_test=_validated_url("PROFILE_API_BASE_URL_TEST", required=False) or base_url,
        base_url_production=_validated_url("PROFILE_API_BASE_URL_PRODUCTION", required=False) or base_url,
        api_key_test=_env_str("PROFILE_API_KEY_TEST") or api_key,
        api_key_production=_env_str("PROFILE_API_KEY_PRODUCTION") or api_key,
        timeout_seconds=_positive_env_int("PROFILE_FAILURE_CALLBACK_TIMEOUT_SECONDS", 10),
    )

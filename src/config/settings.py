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
    """Background task worker: concurrent Dify executions per process (DB claim uses SKIP LOCKED)."""

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
class CommercialQueueConfig:
    """Admission policy for commercial Workers claiming ordinary queued work."""

    threshold: int = 5
    routing_mode: str = "parallel"
    spillover_delay_seconds: int = 0


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
    policy_version: str = "throughput_first_v1"
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
    upload: UploadConfig
    worker: WorkerConfig
    tos: TosConfig
    migration: MigrationConfig
    task_queue_max_waiting: int = 500
    commercial_queue: CommercialQueueConfig = field(default_factory=CommercialQueueConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    runner: ProfileRunnerConfig = field(default_factory=ProfileRunnerConfig)


def _default_task_upload_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "task-uploads"


_SUPPORTED_PLATFORMS = ("dify", "qingtian", "runner")


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


def load_config() -> AppConfig:
    _load_dotenv_from_project_root()
    upload_dir_raw = os.getenv("TASK_UPLOAD_DIR", "").strip()
    upload_base = (
        Path(upload_dir_raw).expanduser().resolve()
        if upload_dir_raw
        else _default_task_upload_dir().resolve()
    )
    workflow_platform = (os.getenv("WORKFLOW_PLATFORM", "dify") or "dify").strip().lower()
    if workflow_platform not in _SUPPORTED_PLATFORMS:
        raise ValueError(
            f"invalid WORKFLOW_PLATFORM: {workflow_platform!r}; "
            f"expected one of {', '.join(_SUPPORTED_PLATFORMS)}"
        )

    dify_required = workflow_platform == "dify"
    qingtian_required = workflow_platform == "qingtian"
    worker_role = _worker_role()
    commercial_queue_config = _commercial_queue_config()
    retry_config = _retry_config()
    work_envs = _worker_work_envs(workflow_platform=workflow_platform)
    runner_config = _runner_config(workflow_platform=workflow_platform, work_envs=work_envs)
    if workflow_platform == "runner":
        if _require_env("WORKFLOW_TARGET_ID") in {"local-default", "commercial-default"}:
            raise ValueError("runner requires an independent WORKFLOW_TARGET_ID")
        if worker_role != "local":
            raise ValueError("runner requires WORKER_ROLE=local")

    legacy_dify_api_key = os.getenv("DIFY_API_KEY", "").strip()
    dify_api_key_test = os.getenv("DIFY_API_KEY_TEST", "").strip() or legacy_dify_api_key
    dify_api_key_production = os.getenv("DIFY_API_KEY_PRODUCTION", "").strip()
    invitation_strategy_api_key = os.getenv("DIFY_INVITATION_STRATEGY_API_KEY", "").strip()
    return AppConfig(
        app_env=os.getenv("APP_ENV", "local").strip() or "local",
        log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
        workflow_platform=workflow_platform,
        dify=DifyConfig(
            base_url=_require_or_empty("DIFY_BASE_URL", dify_required),
            run_url=_require_or_empty("DIFY_RUN_URL", dify_required),
            api_key_test=(
                dify_api_key_test
                if dify_api_key_test or not dify_required
                else _require_env("DIFY_API_KEY_TEST")
            ),
            api_key_production=(
                dify_api_key_production
                if dify_api_key_production or not dify_required
                else _require_env("DIFY_API_KEY_PRODUCTION")
            ),
            invitation_strategy_api_key=(
                invitation_strategy_api_key
                if invitation_strategy_api_key or not dify_required
                else _require_env("DIFY_INVITATION_STRATEGY_API_KEY")
            ),
            user_id=_require_or_empty("DIFY_USER_ID", dify_required),
            image_mode=os.getenv("DIFY_IMAGE_MODE", "file_id").strip() or "file_id",
            stream_init_timeout_ms=_env_int("DIFY_STREAM_INIT_TIMEOUT_MS", 30000),
            poll_interval_ms=_env_int("DIFY_POLL_INTERVAL_MS", 2000),
            poll_timeout_ms=_env_int("DIFY_POLL_TIMEOUT_MS", 2400000),
        ),
        qingtian=QingTianConfig(
            base_url=_require_or_empty("QINGTIAN_BASE_URL", qingtian_required),
            api_key_test=_require_or_empty("QINGTIAN_API_KEY_TEST", qingtian_required),
            api_key_production=_require_or_empty("QINGTIAN_API_KEY_PRODUCTION", qingtian_required),
            invitation_strategy_api_key=_require_or_empty(
                "QINGTIAN_INVITATION_STRATEGY_API_KEY", qingtian_required
            ),
            user_id=_require_or_empty("QINGTIAN_USER_ID", qingtian_required),
            poll_interval_ms=_env_int("QINGTIAN_POLL_INTERVAL_MS", 2000),
            poll_timeout_ms=_env_int("QINGTIAN_POLL_TIMEOUT_MS", 2400000),
        ),
        mysql=load_mysql_config(),
        upload=UploadConfig(
            base_dir=upload_base,
            max_bytes_per_file=_env_int("TASK_UPLOAD_MAX_BYTES_PER_FILE", 10 * 1024 * 1024),
        ),
        worker=WorkerConfig(
            concurrency=_worker_concurrency(),
            poll_interval_seconds=_env_int("WORKER_POLL_INTERVAL_MS", 2000) / 1000.0,
            role=worker_role,
            target_id=_worker_target_id(worker_role),
            target_revision=_worker_target_revision(),
            target_mode=_worker_target_mode(),
            task_types=_worker_task_types(workflow_platform=workflow_platform),
            work_envs=work_envs,
            target_failure_threshold=_worker_target_failure_threshold(),
            target_cooldown_seconds=_worker_target_cooldown_seconds(),
        ),
        tos=TosConfig(
            access_key_id=_env_str("TOS_ACCESS_KEY_ID"),
            access_key_secret=_env_str("TOS_ACCESS_KEY_SECRET"),
            region=_env_str("TOS_REGION"),
            endpoint=_env_str("TOS_ENDPOINT"),
            bucket=_env_str("TOS_BUCKET"),
            presigned_url_expires_seconds=_env_int("TOS_PRESIGNED_URL_EXPIRES_SECONDS", 3600),
        ),
        migration=MigrationConfig(
            db_wait_max_tries=_migration_db_wait_max_tries(),
            db_wait_interval_seconds=_env_int("MIGRATION_DB_WAIT_INTERVAL_MS", 2000) / 1000.0,
        ),
        task_queue_max_waiting=_task_queue_max_waiting(),
        commercial_queue=commercial_queue_config,
        retry=retry_config,
        runner=runner_config,
    )


def _require_or_empty(key: str, required: bool) -> str:
    if required:
        return _require_env(key)
    return _env_str(key, "")


def _worker_concurrency() -> int:
    n = _env_int("WORKER_CONCURRENCY", 8)
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
    default = "customer_profile" if workflow_platform == "runner" else "customer_profile,invitation_strategy"
    values = _csv_values("WORKER_TASK_TYPES", default)
    unsupported = sorted(set(values) - {"customer_profile", "invitation_strategy"})
    if unsupported:
        raise ValueError(f"WORKER_TASK_TYPES contains unsupported values: {', '.join(unsupported)}")
    if workflow_platform == "runner" and "invitation_strategy" in values:
        raise ValueError("runner does not support WORKER_TASK_TYPES=invitation_strategy")
    return values


def _worker_work_envs(*, workflow_platform: str = "dify") -> tuple[str, ...]:
    values = _csv_values("WORKER_WORK_ENVS", "test" if workflow_platform == "runner" else "test,production")
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


def _commercial_queue_config() -> CommercialQueueConfig:
    threshold = _env_int("COMMERCIAL_QUEUE_THRESHOLD", 5)
    spillover_delay_seconds = _env_int("COMMERCIAL_SPILLOVER_DELAY_SECONDS", 0)
    routing_mode = _env_str("WORKER_ROUTING_MODE", "parallel").lower()
    if threshold < 1:
        raise ValueError("COMMERCIAL_QUEUE_THRESHOLD must be >= 1")
    if spillover_delay_seconds < 0:
        raise ValueError("COMMERCIAL_SPILLOVER_DELAY_SECONDS must be >= 0")
    if routing_mode not in {"parallel", "local_preferred", "local_only"}:
        raise ValueError(
            "WORKER_ROUTING_MODE must be one of: parallel, local_preferred, local_only"
        )
    return CommercialQueueConfig(
        threshold=threshold,
        routing_mode=routing_mode,
        spillover_delay_seconds=spillover_delay_seconds,
    )


def _task_queue_max_waiting() -> int:
    n = _env_int("TASK_QUEUE_MAX_WAITING", 500)
    if n < 1:
        raise ValueError("TASK_QUEUE_MAX_WAITING must be >= 1")
    return n


def _retry_config() -> RetryConfig:
    max_poll_recoveries = _env_int("RETRY_MAX_POLL_RECOVERIES", 3)
    max_run_lifetime_seconds = _env_int("RETRY_MAX_RUN_LIFETIME_SECONDS", 7200)
    max_workflow_attempts = _env_int("RETRY_MAX_WORKFLOW_ATTEMPTS", 3)
    policy_version = _env_str("RETRY_POLICY_VERSION", "throughput_first_v1").lower()
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

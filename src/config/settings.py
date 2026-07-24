from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class CommercialQueueConfig:
    """When this many normal tasks are queued, the commercial Worker may help."""

    threshold: int = 5


@dataclass(frozen=True)
class RetryConfig:
    """Repository-owned retry controls for recoverable QingTian workflow runs.

    This is the default QingTian execution path: a returned ``runId`` is
    persisted and can be polled again after a process or network failure.
    No workflow-side change is required.
    """

    max_poll_recoveries: int = 3
    max_run_lifetime_seconds: int = 7200
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


def _default_task_upload_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "task-uploads"


_SUPPORTED_PLATFORMS = ("dify", "qingtian")


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
        mysql=MySQLConfig(
            host=_require_env("MYSQL_HOST"),
            port=_env_int("MYSQL_PORT", 3306),
            user=_require_env("MYSQL_USER"),
            password=_require_env("MYSQL_PASSWORD"),
            database=_require_env("MYSQL_DATABASE"),
        ),
        upload=UploadConfig(
            base_dir=upload_base,
            max_bytes_per_file=_env_int("TASK_UPLOAD_MAX_BYTES_PER_FILE", 10 * 1024 * 1024),
        ),
        worker=WorkerConfig(
            concurrency=_worker_concurrency(),
            poll_interval_seconds=_env_int("WORKER_POLL_INTERVAL_MS", 2000) / 1000.0,
            role=worker_role,
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


def _commercial_queue_config() -> CommercialQueueConfig:
    threshold = _env_int("COMMERCIAL_QUEUE_THRESHOLD", 5)
    if threshold < 1:
        raise ValueError("COMMERCIAL_QUEUE_THRESHOLD must be >= 1")
    return CommercialQueueConfig(threshold=threshold)


def _task_queue_max_waiting() -> int:
    n = _env_int("TASK_QUEUE_MAX_WAITING", 500)
    if n < 1:
        raise ValueError("TASK_QUEUE_MAX_WAITING must be >= 1")
    return n


def _retry_config() -> RetryConfig:
    max_poll_recoveries = _env_int("RETRY_MAX_POLL_RECOVERIES", 3)
    max_run_lifetime_seconds = _env_int("RETRY_MAX_RUN_LIFETIME_SECONDS", 7200)
    if max_poll_recoveries < 1:
        raise ValueError("RETRY_MAX_POLL_RECOVERIES must be >= 1")
    if max_run_lifetime_seconds < 1:
        raise ValueError("RETRY_MAX_RUN_LIFETIME_SECONDS must be >= 1")
    return RetryConfig(
        max_poll_recoveries=max_poll_recoveries,
        max_run_lifetime_seconds=max_run_lifetime_seconds,
    )


def _migration_db_wait_max_tries() -> int:
    n = _env_int("MIGRATION_DB_WAIT_MAX_TRIES", 30)
    if n < 1:
        raise ValueError("MIGRATION_DB_WAIT_MAX_TRIES must be >= 1")
    return n

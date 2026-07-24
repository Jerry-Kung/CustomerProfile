from __future__ import annotations

import os

import pytest

import src.config.settings as settings
from src.config.settings import load_config


REQUIRED_ENV = {
    "DIFY_BASE_URL": "http://118.145.238.55",
    "DIFY_RUN_URL": "http://118.145.238.55/v1/workflows/run",
    "DIFY_API_KEY": "app-BARxxxxxxxx",
    "DIFY_API_KEY_PRODUCTION": "app-PROD-xxxx",
    "DIFY_INVITATION_STRATEGY_API_KEY": "app-INV-xxxx",
    "DIFY_USER_ID": "klh",
    "MYSQL_HOST": "118.145.238.55",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "root",
    "MYSQL_PASSWORD": "123456xxxx",
    "MYSQL_DATABASE": "customer_profile",
}


def _load_with_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None):
    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: None)
    for key in list(os.environ.keys()):
        if (
            key.startswith("DIFY_")
            or key.startswith("QINGTIAN_")
            or key.startswith("MYSQL_")
            or key.startswith("WORKER_")
            or key.startswith("RETRY_")
            or key.startswith("COMMERCIAL_")
            or key in {"APP_ENV", "LOG_LEVEL", "WORKFLOW_PLATFORM"}
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    if extra:
        for key, value in extra.items():
            monkeypatch.setenv(key, value)
    return load_config()


def test_load_config_success_defaults(monkeypatch: pytest.MonkeyPatch):
    config = _load_with_env(monkeypatch)
    assert config.app_env == "local"
    assert config.log_level == "INFO"
    assert config.dify.image_mode == "file_id"
    assert config.dify.stream_init_timeout_ms == 30000
    assert config.dify.poll_interval_ms == 2000
    # poll_timeout_ms may be overridden by a present .env; only verify it is a positive int
    assert isinstance(config.dify.poll_timeout_ms, int) and config.dify.poll_timeout_ms > 0
    assert config.mysql.port == 3306
    assert config.worker.concurrency >= 1
    assert config.worker.role == "local"
    assert config.commercial_queue.threshold == 5
    assert config.retry.max_poll_recoveries == 3
    assert config.retry.max_run_lifetime_seconds == 7200
    assert config.retry.backoff_seconds == (30, 120, 600)
    assert config.dify.invitation_strategy_api_key == "app-INV-xxxx"


def test_load_config_overrides(monkeypatch: pytest.MonkeyPatch):
    config = _load_with_env(
        monkeypatch,
        {
            "APP_ENV": "test",
            "LOG_LEVEL": "DEBUG",
            "DIFY_IMAGE_MODE": "multipart",
            "DIFY_STREAM_INIT_TIMEOUT_MS": "45000",
            "DIFY_POLL_INTERVAL_MS": "3000",
            "DIFY_POLL_TIMEOUT_MS": "600000",
            "MYSQL_PORT": "3307",
            "WORKER_ROLE": "commercial",
            "COMMERCIAL_QUEUE_THRESHOLD": "20",
            "RETRY_MAX_POLL_RECOVERIES": "4",
            "RETRY_MAX_RUN_LIFETIME_SECONDS": "9000",
            "RETRY_BACKOFF_SECONDS": "5,10",
            "RETRY_LEASE_SECONDS": "5",
            "RETRY_LEASE_HEARTBEAT_SECONDS": "4",
            "RETRY_RECOVERY_INTERVAL_SECONDS": "5",
        },
    )
    assert config.app_env == "test"
    assert config.log_level == "DEBUG"
    assert config.dify.image_mode == "multipart"
    assert config.dify.stream_init_timeout_ms == 45000
    assert config.dify.poll_interval_ms == 3000
    assert config.dify.poll_timeout_ms == 600000
    assert config.mysql.port == 3307
    assert config.worker.role == "commercial"
    assert config.commercial_queue.threshold == 20
    assert config.retry.max_poll_recoveries == 4
    assert config.retry.max_run_lifetime_seconds == 9000
    # Internal retry coordination values are fixed in code and stale
    # deployment environment variables must not override them.
    assert config.retry.backoff_seconds == (30, 120, 600)
    assert config.retry.lease_seconds == 180
    assert config.retry.lease_heartbeat_seconds == 30
    assert config.retry.recovery_interval_seconds == 30


def test_load_config_rejects_invalid_worker_role(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="WORKER_ROLE"):
        _load_with_env(monkeypatch, {"WORKER_ROLE": "random"})


def test_load_config_rejects_non_positive_commercial_queue_threshold(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ValueError, match="COMMERCIAL_QUEUE_THRESHOLD"):
        _load_with_env(monkeypatch, {"COMMERCIAL_QUEUE_THRESHOLD": "0"})


def test_load_config_missing_required(monkeypatch: pytest.MonkeyPatch):
    _load_with_env(monkeypatch)
    monkeypatch.delenv("DIFY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="missing required environment variable: DIFY_API_KEY"):
        load_config()


def test_load_config_invalid_int(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ.keys()):
        if (
            key.startswith("DIFY_")
            or key.startswith("MYSQL_")
            or key.startswith("RETRY_")
            or key in {"APP_ENV", "LOG_LEVEL"}
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MYSQL_PORT", "invalid")
    with pytest.raises(ValueError, match="invalid integer value for MYSQL_PORT"):
        load_config()


def test_load_config_rejects_non_positive_retry_run_bounds(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="RETRY_MAX_POLL_RECOVERIES"):
        _load_with_env(monkeypatch, {"RETRY_MAX_POLL_RECOVERIES": "0"})
    with pytest.raises(ValueError, match="RETRY_MAX_RUN_LIFETIME_SECONDS"):
        _load_with_env(monkeypatch, {"RETRY_MAX_RUN_LIFETIME_SECONDS": "0"})

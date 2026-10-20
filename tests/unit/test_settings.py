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
            or key.startswith("WORKFLOW_")
            or key.startswith("RETRY_")
            or key.startswith("COMMERCIAL_")
            or key.startswith("PROFILE_RUNNER_")
            or key.startswith("PROFILE_ROUTING_")
            or key in {"APP_ENV", "LOG_LEVEL", "WORKFLOW_PLATFORM"}
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    if extra:
        for key, value in extra.items():
            monkeypatch.setenv(key, value)
    return load_config()


def _runner_env(**overrides):
    env = {"WORKFLOW_PLATFORM": "runner", "WORKFLOW_TARGET_ID": "runner-profile-prod-v1",
           "WORKER_WORK_ENVS": "production", "PROFILE_RUNNER_BASE_URL_PRODUCTION": "http://runner:18000"}
    env.update(overrides)
    return env


def test_runner_configuration_requires_only_selected_environment(monkeypatch):
    config = _load_with_env(monkeypatch, _runner_env())
    assert config.worker.task_types == ("customer_profile",)
    assert config.worker.work_envs == ("production",)
    assert config.runner.base_url_test == ""
    assert config.runner.base_url_production == "http://runner:18000"
    assert config.runner.request_timeout_ms == 15000
    assert config.runner.poll_interval_ms == 2000
    assert config.runner.poll_timeout_ms == 2400000
    assert config.retry.policy_version == "throughput_first_v1"


@pytest.mark.parametrize("extra", [
    {"PROFILE_ROUTING_MODE": "weighted", "PROFILE_RUNNER_TRAFFIC_PERCENT": "80",
     "PROFILE_ROUTING_SPILLOVER_SECONDS": "300", "PROFILE_ROUTING_WORK_ENVS": "production"},
    {"PROFILE_ROUTING_MODE": "random"}, {"PROFILE_RUNNER_TRAFFIC_PERCENT": "101"},
    {"PROFILE_RUNNER_TRAFFIC_PERCENT": "-1"}, {"PROFILE_ROUTING_SPILLOVER_SECONDS": "0"},
    {"PROFILE_ROUTING_WORK_ENVS": "invalid"},
])
def test_retired_percentage_settings_do_not_change_worker_configuration(monkeypatch, extra):
    baseline = _load_with_env(monkeypatch, _runner_env(WORKER_CONCURRENCY="3"))
    config = _load_with_env(monkeypatch, _runner_env(WORKER_CONCURRENCY="3", **extra))
    assert config == baseline
    assert config.worker.concurrency == 3


def test_runner_default_environment_is_test_and_never_falls_back_to_production(monkeypatch):
    with pytest.raises(ValueError, match="PROFILE_RUNNER_BASE_URL_TEST"):
        _load_with_env(monkeypatch, {"WORKFLOW_PLATFORM": "runner", "WORKFLOW_TARGET_ID": "runner-v1",
                                     "PROFILE_RUNNER_BASE_URL_PRODUCTION": "http://runner"})


@pytest.mark.parametrize("overrides, error", [
    ({"WORKFLOW_TARGET_ID": ""}, "WORKFLOW_TARGET_ID"),
    ({"WORKFLOW_TARGET_ID": "local-default"}, "independent WORKFLOW_TARGET_ID"),
    ({"WORKFLOW_TARGET_ID": "commercial-default"}, "independent WORKFLOW_TARGET_ID"),
    ({"WORKER_TASK_TYPES": "customer_profile,invitation_strategy"}, "invitation_strategy"),
    ({"WORKER_ROLE": "commercial"}, "WORKER_ROLE"),
    ({"PROFILE_RUNNER_BASE_URL_PRODUCTION": "http://user:password@runner"}, "base URL"),
    ({"PROFILE_RUNNER_BASE_URL_PRODUCTION": "http://runner/?key=value"}, "base URL"),
    ({"PROFILE_RUNNER_REQUEST_TIMEOUT_MS": "0"}, "PROFILE_RUNNER_REQUEST_TIMEOUT_MS"),
    ({"PROFILE_RUNNER_POLL_INTERVAL_MS": "-1"}, "PROFILE_RUNNER_POLL_INTERVAL_MS"),
    ({"PROFILE_RUNNER_POLL_TIMEOUT_MS": "0"}, "PROFILE_RUNNER_POLL_TIMEOUT_MS"),
])
def test_runner_invalid_configuration_is_rejected(monkeypatch, overrides, error):
    with pytest.raises(ValueError, match=error):
        _load_with_env(monkeypatch, _runner_env(**overrides))


@pytest.mark.parametrize("policy", ["throughput_first_v1", "local_first_v1", "single_target_v1"])
def test_runner_accepts_existing_retry_policy(monkeypatch, policy):
    config = _load_with_env(monkeypatch, _runner_env(RETRY_POLICY_VERSION=policy))
    assert config.retry.policy_version == policy




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
    assert config.worker.target_id == "local-default"
    assert config.worker.target_revision == "v1"
    assert config.worker.target_mode == "active"
    assert config.worker.target_failure_threshold == 3
    assert config.worker.target_cooldown_seconds == 60
    assert config.commercial_queue.routing_mode == "parallel"
    assert config.commercial_queue.spillover_delay_seconds == 0
    assert config.commercial_queue.threshold == 5
    assert config.retry.max_poll_recoveries == 3
    assert config.retry.max_run_lifetime_seconds == 7200
    assert config.retry.max_workflow_attempts == 3
    assert config.retry.policy_version == "throughput_first_v1"
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
            "WORKFLOW_TARGET_ID": "commercial-02",
            "WORKFLOW_TARGET_REVISION": "key-v2",
            "WORKER_TARGET_MODE": "draining",
            "WORKER_TASK_TYPES": "customer_profile",
            "WORKER_WORK_ENVS": "production",
            "WORKER_TARGET_FAILURE_THRESHOLD": "5",
            "WORKER_TARGET_COOLDOWN_SECONDS": "90",
            "WORKER_ROUTING_MODE": "local_preferred",
            "COMMERCIAL_QUEUE_THRESHOLD": "20",
            "COMMERCIAL_SPILLOVER_DELAY_SECONDS": "45",
            "RETRY_MAX_POLL_RECOVERIES": "4",
            "RETRY_MAX_RUN_LIFETIME_SECONDS": "9000",
            "RETRY_MAX_WORKFLOW_ATTEMPTS": "4",
            "RETRY_POLICY_VERSION": "local_first_v1",
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
    assert config.worker.target_id == "commercial-02"
    assert config.worker.target_revision == "key-v2"
    assert config.worker.target_mode == "draining"
    assert config.worker.task_types == ("customer_profile",)
    assert config.worker.work_envs == ("production",)
    assert config.worker.target_failure_threshold == 5
    assert config.worker.target_cooldown_seconds == 90
    assert config.commercial_queue.threshold == 20
    assert config.commercial_queue.routing_mode == "local_preferred"
    assert config.commercial_queue.spillover_delay_seconds == 45
    assert config.retry.max_poll_recoveries == 4
    assert config.retry.max_run_lifetime_seconds == 9000
    assert config.retry.max_workflow_attempts == 4
    assert config.retry.policy_version == "local_first_v1"
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
    with pytest.raises(ValueError, match="RETRY_MAX_WORKFLOW_ATTEMPTS"):
        _load_with_env(monkeypatch, {"RETRY_MAX_WORKFLOW_ATTEMPTS": "0"})


def test_load_config_rejects_invalid_target_and_routing_policy(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ValueError, match="WORKFLOW_TARGET_ID"):
        _load_with_env(monkeypatch, {"WORKFLOW_TARGET_ID": "bad target"})
    with pytest.raises(ValueError, match="WORKER_ROUTING_MODE"):
        _load_with_env(monkeypatch, {"WORKER_ROUTING_MODE": "random"})
    with pytest.raises(ValueError, match="RETRY_POLICY_VERSION"):
        _load_with_env(monkeypatch, {"RETRY_POLICY_VERSION": "random"})


def test_load_config_accepts_single_target_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _load_with_env(
        monkeypatch,
        {"RETRY_POLICY_VERSION": "single_target_v1"},
    )
    assert config.retry.policy_version == "single_target_v1"


def test_load_config_rejects_invalid_target_circuit_bounds(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ValueError, match="WORKER_TARGET_FAILURE_THRESHOLD"):
        _load_with_env(
            monkeypatch,
            {"WORKER_TARGET_FAILURE_THRESHOLD": "0"},
        )
    with pytest.raises(ValueError, match="WORKER_TARGET_COOLDOWN_SECONDS"):
        _load_with_env(
            monkeypatch,
            {"WORKER_TARGET_COOLDOWN_SECONDS": "0"},
        )

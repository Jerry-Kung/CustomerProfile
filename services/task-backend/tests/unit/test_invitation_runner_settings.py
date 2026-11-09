import json
import logging
import os

import pytest

from src.app.bootstrap import _build_workflow_client, _workflow_poll_timeout_ms
from src.config import settings
from src.config.logging_utils import redact_config
from src.config.settings import load_config
from src.infrastructure.invitation_runner.client import InvitationRunnerClient


@pytest.fixture
def native_env(monkeypatch):
    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: None)
    for key in list(os.environ):
        if key.startswith(
            (
                "WORKFLOW_",
                "WORKER_",
                "PROFILE_",
                "INVITATION_RUNNER_",
                "QINGTIAN_",
                "DIFY_",
                "MYSQL_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in {
        "WORKFLOW_PLATFORM": "invitation_runner",
        "WORKFLOW_TARGET_ID": "native-invitation-test",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_USER": "unused",
        "MYSQL_PASSWORD": "offline-password",
        "MYSQL_DATABASE": "unused",
        "INVITATION_RUNNER_BASE_URL_TEST": "http://invitation-test:8099",
        "INVITATION_RUNNER_API_KEY_TEST": "offline-inbound-secret",
        "PROFILE_FAILURE_CALLBACK_ENABLED": "true",
        "PROFILE_API_BASE_URL": "http://business-test",
        "PROFILE_API_KEY": "offline-business-secret",
    }.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_invitation_worker_uses_its_own_adapter_and_scope(native_env):
    config = load_config()
    assert config.worker.task_types == (
        "invitation_strategy",
    ) and config.worker.work_envs == ("test",)
    assert config.invitation_runner.api_key_test == "offline-inbound-secret"
    assert (
        config.invitation_runner.base_url_production
        == config.invitation_runner.api_key_production
        == ""
    )
    assert (
        config.qingtian.invitation_strategy_api_key == config.runner.base_url_test == ""
    )
    assert isinstance(
        _build_workflow_client(config, logging.getLogger("test")),
        InvitationRunnerClient,
    )
    assert _workflow_poll_timeout_ms(config) == config.invitation_runner.poll_timeout_ms
    assert "offline-inbound-secret" not in json.dumps(redact_config(config))
    assert "offline-inbound-secret" not in repr(config.invitation_runner)


@pytest.mark.parametrize(
    "key,value,error",
    [
        ("WORKER_TASK_TYPES", "customer_profile", "WORKER_TASK_TYPES"),
        (
            "WORKER_TASK_TYPES",
            "customer_profile,invitation_strategy",
            "WORKER_TASK_TYPES",
        ),
        ("WORKFLOW_TARGET_ID", "local-default", "independent"),
        ("INVITATION_RUNNER_API_KEY_TEST", "", "INVITATION_RUNNER_API_KEY_TEST"),
        ("INVITATION_RUNNER_BASE_URL_TEST", "", "INVITATION_RUNNER_BASE_URL_TEST"),
        (
            "INVITATION_RUNNER_BASE_URL_TEST",
            "http://user:secret@invitation",
            "base URL",
        ),
        (
            "INVITATION_RUNNER_BASE_URL_TEST",
            "http://invitation/?token=secret",
            "base URL",
        ),
        ("INVITATION_RUNNER_POLL_TIMEOUT_MS", "0", "POLL_TIMEOUT"),
        (
            "PROFILE_FAILURE_CALLBACK_ENABLED",
            "false",
            "PROFILE_FAILURE_CALLBACK_ENABLED",
        ),
    ],
)
def test_invalid_invitation_configuration_fails_at_startup(
    native_env, key, value, error
):
    native_env.setenv(key, value)
    with pytest.raises(ValueError, match=error):
        load_config()


def test_production_never_falls_back_to_test_configuration(native_env):
    native_env.setenv("WORKER_WORK_ENVS", "production")
    with pytest.raises(ValueError, match="INVITATION_RUNNER_BASE_URL_PRODUCTION"):
        load_config()
    native_env.setenv(
        "INVITATION_RUNNER_BASE_URL_PRODUCTION", "http://invitation-prod:8099"
    )
    with pytest.raises(ValueError, match="INVITATION_RUNNER_API_KEY_PRODUCTION"):
        load_config()
    native_env.setenv("INVITATION_RUNNER_API_KEY_PRODUCTION", "offline-prod-key")
    assert load_config().invitation_runner.api_key_production == "offline-prod-key"


@pytest.mark.parametrize("component", ["api", "migration"])
def test_non_workers_do_not_require_invitation_credentials(native_env, component):
    native_env.delenv("INVITATION_RUNNER_API_KEY_TEST")
    native_env.delenv("INVITATION_RUNNER_BASE_URL_TEST")
    native_env.setenv("PROFILE_FAILURE_CALLBACK_ENABLED", "false")
    assert load_config(component=component).invitation_runner.api_key_test == ""

"""Render real Compose interpolation without starting containers or reading production env files."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = {
    "MYSQL_HOST": "invalid.example", "MYSQL_USER": "test", "MYSQL_PASSWORD": "test-only",
    "MYSQL_DATABASE": "test", "QINGTIAN_BASE_URL": "https://qt.example",
    "QINGTIAN_USER_ID": "test", "QINGTIAN_API_KEY_TEST": "test-only",
    "QINGTIAN_API_KEY_PRODUCTION": "test-only", "QINGTIAN_INVITATION_STRATEGY_API_KEY": "test-only",
    "QINGTIAN_COMMERCIAL_API_KEY_TEST": "commercial-test-only",
    "QINGTIAN_COMMERCIAL_API_KEY_PRODUCTION": "commercial-test-only",
    "PROFILE_RUNNER_BASE_URL_PRODUCTION": "http://runner.example:18000",
}


def render(tmp_path, overrides=None, *, additional=False, native_invitation=False):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed; no daemon is needed")
    env_file = tmp_path / "worker env"
    env_file.write_text("\n".join(k + "=" + v for k, v in (BASE | (overrides or {})).items()))
    runtime = {k: v for k, v in os.environ.items() if not k.startswith((
        "COMPOSE_", "QINGTIAN_", "PROFILE_", "INVITATION_RUNNER_", "COMMERCIAL_", "WORKER_", "LOCAL_", "API_", "HOST_API_"))}
    runtime["APP_ENV_FILE"] = str(env_file)
    command = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_file),
               "-f", str(ROOT / "docker-compose.yml")]
    if additional:
        command += ["-f", str(ROOT / "deploy/examples/additional-worker.compose.yaml")]
    if native_invitation:
        command += ["--profile", "invitation-runner"]
    result = subprocess.run(command + ["config", "--format", "json"], env=runtime,
                            text=True, capture_output=True)
    assert result.returncode == 0, "Compose rendering failed (output withheld to protect env credentials)"
    return json.loads(result.stdout)["services"]


def test_all_worker_groups_have_independent_scope_and_concurrency(tmp_path):
    services = render(tmp_path, {
        "WORKER_CONCURRENCY": "99", "QINGTIAN_WORKER_CONCURRENCY": "2",
        "QINGTIAN_WORKER_TASK_TYPES": "invitation_strategy", "QINGTIAN_WORKER_WORK_ENVS": "test",
        "COMMERCIAL_WORKER_CONCURRENCY": "3", "COMMERCIAL_WORKER_TARGET_MODE": "active",
        "PROFILE_RUNNER_CONCURRENCY": "5", "HOST_API_PORT": "18007", "API_PORT": "18009",
    })
    qt, commercial, runner = (services[n]["environment"] for n in
                              ("worker", "commercial-worker", "profile-runner-worker"))
    assert (qt["WORKER_CONCURRENCY"], commercial["WORKER_CONCURRENCY"], runner["WORKER_CONCURRENCY"]) == ("2", "3", "5")
    assert qt["WORKER_TASK_TYPES"] == "invitation_strategy" and qt["WORKER_WORK_ENVS"] == "test"
    assert commercial["WORKFLOW_PLATFORM"] == qt["WORKFLOW_PLATFORM"] == "qingtian"
    assert commercial["WORKER_TARGET_MODE"] == "active"
    assert runner["WORKFLOW_PLATFORM"] == "runner" and runner["WORKER_WORK_ENVS"] == "production"
    assert services["api"]["ports"][0]["published"] == "18007"
    assert services["api"]["ports"][0]["target"] == 18009
    for name in ("worker", "commercial-worker", "profile-runner-worker"):
        assert "container_name" not in services[name] and "ports" not in services[name]
    # No API override silently changes the task policy supplied by the operator.
    assert "RETRY_POLICY_VERSION" not in services["api"]["environment"]


def test_legacy_concurrency_and_mode_inputs_remain_explicitly_compatible(tmp_path):
    services = render(tmp_path, {"WORKER_CONCURRENCY": "2", "LOCAL_WORKER_TARGET_MODE": "draining",
                                 "COMMERCIAL_1_WORKER_TARGET_MODE": "recovery_only"})
    assert services["worker"]["environment"]["WORKER_CONCURRENCY"] == "2"
    assert services["worker"]["environment"]["WORKER_TARGET_MODE"] == "draining"
    assert services["commercial-worker"]["environment"]["WORKER_TARGET_MODE"] == "recovery_only"


def test_native_cutover_defaults_drain_old_targets_and_allocate_five_slots(tmp_path):
    services = render(tmp_path, {
        "INVITATION_RUNNER_BASE_URL_TEST": "http://native-invitation:8099",
        "INVITATION_RUNNER_API_KEY_TEST": "offline-only",
    }, native_invitation=True)
    for name, target in (("worker", "local-default"), ("commercial-worker", "commercial-default")):
        env = services[name]["environment"]
        assert env["WORKER_TARGET_MODE"] == "draining"
        assert env["WORKFLOW_TARGET_ID"] == target
        assert env["WORKFLOW_TARGET_REVISION"] == "v1"
    assert services["worker"]["environment"]["WORKER_TASK_TYPES"] == "customer_profile"
    for name in ("profile-runner-worker", "invitation-runner-worker"):
        assert services[name]["environment"]["WORKER_CONCURRENCY"] == "5"
        assert services[name]["environment"]["WORKER_TARGET_MODE"] == "active"


def test_distinct_qingtian_target_can_be_added_without_new_runtime_code(tmp_path):
    services = render(tmp_path, {"QINGTIAN_B_CONCURRENCY": "4", "QINGTIAN_B_API_KEY_PRODUCTION": "b-test-only"}, additional=True)
    worker = services["qingtian-b"]["environment"]
    assert worker["WORKFLOW_TARGET_ID"] == "qingtian-b"
    assert worker["WORKER_CONCURRENCY"] == "4"
    assert worker["WORKER_TASK_TYPES"] == "customer_profile"
    assert worker["WORKER_WORK_ENVS"] == "production"
    assert worker["QINGTIAN_API_KEY_PRODUCTION"] == "b-test-only"


def test_wrapper_uses_one_env_file_for_interpolation_and_injection(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps([sys.argv[1:],os.environ['APP_ENV_FILE']]))\n")
    docker.chmod(0o755)
    env_file = tmp_path / "custom env"
    env_file.write_text("WORKER_CONCURRENCY=2\n")
    result = subprocess.run([str(ROOT / "scripts/compose.sh"), "--env-file", str(env_file), "config", "--services"],
                            env=os.environ | {"PATH": str(binary) + os.pathsep + os.environ["PATH"]},
                            capture_output=True, text=True, check=True)
    arguments, injected_path = json.loads(result.stdout)
    assert arguments[arguments.index("--env-file") + 1] == injected_path == str(env_file)
    assert arguments[-2:] == ["config", "--services"]


def test_native_invitation_group_is_opt_in_and_has_an_independent_target(tmp_path):
    assert "invitation-runner-worker" not in render(tmp_path)
    services = render(tmp_path, {"INVITATION_RUNNER_CONCURRENCY": "4", "INVITATION_RUNNER_WORK_ENVS": "test",
        "INVITATION_RUNNER_BASE_URL_TEST": "http://native-invitation:8099", "INVITATION_RUNNER_API_KEY_TEST": "offline-only"},
        native_invitation=True)
    worker = services["invitation-runner-worker"]["environment"]
    assert worker["WORKFLOW_PLATFORM"] == "invitation_runner"
    assert worker["WORKER_TASK_TYPES"] == "invitation_strategy"
    assert worker["WORKER_WORK_ENVS"] == "test" and worker["WORKER_CONCURRENCY"] == "4"
    assert worker["WORKFLOW_TARGET_ID"] != services["profile-runner-worker"]["environment"]["WORKFLOW_TARGET_ID"]
    assert worker["INVITATION_RUNNER_API_KEY_TEST"] == "offline-only"

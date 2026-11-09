"""Parse the real Compose definition without contacting Docker Engine."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="requires Docker Compose CLI")


def test_one_image_with_separate_credentials_ports_and_existing_history_volume(tmp_path):
    runtime = tmp_path / "runtime.env"
    task = tmp_path / "task.env"
    runtime.write_text(
        "API_PORT=18175\nCP_API_PORT=18000\nPROFILE_API_KEY=runtime-fixture-key\n",
        encoding="utf-8",
    )
    task.write_text(
        "API_PORT=19999\nTASK_API_PORT=18009\nHOST_API_PORT=18007\n"
        "PROFILE_API_KEY=task-fixture-key\nPROFILE_RUNNER_CONCURRENCY=5\n",
        encoding="utf-8",
    )
    image = "customer-profile-unified:20261109-150000-test"
    result = subprocess.run([
        str(ROOT / "scripts/compose.sh"), "--env-file", str(runtime),
        "--task-env-file", str(task), "--profile", "*", "config", "--format", "json",
    ], env=dict(os.environ, CP_IMAGE=image), capture_output=True, text=True, check=True)
    configuration = json.loads(result.stdout)
    services = configuration["services"]
    assert len(services) == 10
    assert {service["image"] for service in services.values()} == {image}
    assert services["api"]["environment"]["PROFILE_API_KEY"] == "runtime-fixture-key"
    assert services["task-api"]["environment"]["PROFILE_API_KEY"] == "task-fixture-key"
    assert services["api"]["environment"]["API_PORT"] == "18175"
    assert services["task-api"]["environment"]["API_PORT"] == "18009"
    assert services["task-api"]["ports"][0]["published"] == "18007"
    assert services["profile-worker"]["environment"]["WORKER_CONCURRENCY"] == ""
    assert services["profile-runner-worker"]["environment"]["WORKER_CONCURRENCY"] == "5"
    assert services["task-api"]["working_dir"] == "/app/services/task-backend"
    assert services["task-api"]["environment"]["PYTHONPATH"] == "/app/services/task-backend"
    assert configuration["volumes"]["profile-data"]["name"] == "customer-profile_profile-data"
    assert services["api"]["environment"]["INTERRUPT_ON_START"] == "false"
    assert services["profile-worker"]["stop_grace_period"] == "1h0m0s"
    assert services["invitation-worker"]["stop_grace_period"] == "1h0m0s"


@pytest.mark.parametrize("custom_file", [False, True])
def test_one_env_file_preserves_project_ports_and_worker_scopes(tmp_path, custom_file):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/compose.sh", scripts / "compose.sh")
    shutil.copy2(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    env_file = tmp_path / ("custom config.env" if custom_file else ".env")
    env_file.write_text(
        "COMPOSE_PROJECT_NAME=customer-profile-runner\n"
        "API_PORT=18173\nCP_API_PORT=18000\nINBOUND_PORT=8099\n"
        "TASK_API_PORT=18008\nTASK_HOST_API_PORT=18007\n"
        "PROFILE_API_KEY=shared-fixture-key\nMYSQL_PASSWORD=mysql-fixture-key\n"
        "WORKER_CONCURRENCY=10\nPROFILE_RUNNER_CONCURRENCY=5\n"
        "PROFILE_RUNNER_WORK_ENVS=test,production\n"
        "INVITATION_RUNNER_TARGET_ID=invitation-runner-uat\n"
        "INVITATION_RUNNER_WORK_ENVS=test,production\n",
        encoding="utf-8",
    )
    env = dict(os.environ, CP_IMAGE="customer-profile-unified:20261109-150000-test")
    for key in ("CP_ENV_FILE", "TASK_ENV_FILE", "COMPOSE_PROJECT_NAME"):
        env.pop(key, None)
    result = subprocess.run([
        str(scripts / "compose.sh"),
        *(["--env-file", str(env_file)] if custom_file else []),
        "--profile", "invitation-runner", "config", "--format", "json",
    ], env=env, capture_output=True, text=True, check=True)
    config = json.loads(result.stdout)
    services = config["services"]
    assert config["name"] == "customer-profile-runner"
    assert config["volumes"]["profile-data"]["name"] == "customer-profile-runner_profile-data"
    assert services["api"]["environment"]["API_PORT"] == "18173"
    assert services["api"]["ports"][0]["published"] == "18000"
    assert services["task-api"]["environment"]["API_PORT"] == "18008"
    assert services["task-api"]["ports"][0]["published"] == "18007"
    assert services["task-api"]["environment"]["MYSQL_PASSWORD"] == "mysql-fixture-key"
    assert services["profile-worker"]["environment"]["WORKER_CONCURRENCY"] == ""
    assert services["profile-runner-worker"]["environment"]["WORKER_CONCURRENCY"] == "5"
    assert services["invitation-runner-worker"]["environment"]["WORKFLOW_TARGET_ID"] == "invitation-runner-uat"
    assert services["invitation-runner-worker"]["environment"]["WORKER_WORK_ENVS"] == "test,production"

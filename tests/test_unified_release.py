"""Exercise the release CLI with a local Docker stand-in and temporary Git checkout."""

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "example/customer-profile:20261109-150000-test"


@pytest.fixture
def release_project(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("release.py", "compose.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (tmp_path / "compose.yaml").write_text("name: customer-profile\n", encoding="utf-8")
    runtime_env = tmp_path / "runtime config.env"
    task_env = tmp_path / "task config.env"
    runtime_env.write_text("API_PORT=18173\n", encoding="utf-8")
    task_env.write_text("API_PORT=18008\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "scripts", "compose.yaml"], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Release Test",
        "-c", "user.email=release@example.invalid", "commit", "-qm", "fixture",
    ], check=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        "#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["DOCKER_CALLS"]).open("a") as stream:
    stream.write(json.dumps({"args": args, "runtime_env": os.getenv("CP_ENV_FILE"), "task_env": os.getenv("TASK_ENV_FILE")}) + "\\n")
if args[0] == "info":
    print("linux/aarch64")
elif args[:2] == ["image", "inspect"] and "--format" in args:
    print(json.dumps({"io.customer-profile.layout": "v1", "org.opencontainers.image.revision": "fixture-image-revision"}))
elif args[0] == "compose":
    index = next(i for i, item in enumerate(args) if item in {"config", "ps", "stop", "run", "up"})
    action = args[index:]
    if action[0] == "config" and "--format" in action:
        names = ["api", "profile-worker", "inbound", "invitation-worker", "task-api", "qingtian-worker", "commercial-worker", "profile-runner-worker"]
        if "invitation-runner" in args:
            names.append("invitation-runner-worker")
        print(json.dumps({"name": "customer-profile", "services": {name: {"image": os.environ["CP_IMAGE"]} for name in names}}))
    elif action[0] == "ps":
        print(json.dumps([{"Service": "worker", "State": "running"}]) if os.getenv("LEGACY_WORKER") else "[]")
    elif action[0] == "run" and "--entrypoint" in action and os.getenv("FAIL_PREFLIGHT"):
        sys.exit(1)
    elif action[0] == "up" and os.getenv("FAIL_EXECUTION") and "api" in action:
        sys.exit(1)
''',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    calls = tmp_path / "docker.jsonl"
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"], DOCKER_CALLS=str(calls))
    env.pop("CP_IMAGE", None)
    env.pop("COMPOSE_PROFILES", None)

    def invoke(*arguments, failure=False, preflight_failure=False, build=False, legacy_worker=False):
        selected_env = dict(env)
        if failure:
            selected_env["FAIL_EXECUTION"] = "1"
        if preflight_failure:
            selected_env["FAIL_PREFLIGHT"] = "1"
        if legacy_worker:
            selected_env["LEGACY_WORKER"] = "1"
        result = subprocess.run([
            sys.executable, str(scripts / "release.py"),
            "--env-file", str(runtime_env), "--task-env-file", str(task_env),
            "--image", IMAGE, *([] if build else ["--skip-build"]), *arguments,
        ], cwd=tmp_path, env=selected_env, capture_output=True, text=True)
        rows = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, rows

    return tmp_path, runtime_env, task_env, invoke


def operations(rows):
    found = []
    for row in rows:
        args = row["args"]
        if args[0] == "compose":
            for index, item in enumerate(args):
                if item in {"stop", "run", "up"}:
                    action = args[index:]
                    if "--entrypoint" not in action:
                        found.append(action)
                    break
    return found


def test_all_release_drains_before_update_and_resumes_after_health(release_project):
    project, runtime_env, task_env, invoke = release_project
    result, rows = invoke("--profile", "invitation-runner", "--migrate")
    assert result.returncode == 0, result.stderr
    actions = operations(rows)
    assert actions[0] == ["stop", "qingtian-worker", "commercial-worker", "profile-runner-worker", "invitation-runner-worker"]
    assert actions[1] == ["stop", "profile-worker", "invitation-worker"]
    assert actions[2] == ["run", "--rm", "--no-deps", "task-migrate"]
    assert actions[3][-4:] == ["api", "profile-worker", "inbound", "invitation-worker"]
    assert actions[4][-1] == "task-api"
    assert actions[5][-4:] == actions[0][1:]
    assert all("--wait" in action and "--no-build" in action for action in actions[3:])
    for row in rows:
        if row["args"][0] == "compose":
            assert row["runtime_env"] == str(runtime_env)
            assert row["task_env"] == str(task_env)
            files = [row["args"][i + 1] for i, value in enumerate(row["args"]) if value == "--env-file"]
            assert files == [str(task_env), str(runtime_env)]
    record = json.loads(next((project / ".releases").glob("*.json")).read_text())
    assert record["status"] == "succeeded"
    assert record["image"] == IMAGE
    assert record["revision"] == "fixture-image-revision"


def test_profile_update_has_no_invitation_or_task_api_restart(release_project):
    _, _, _, invoke = release_project
    result, rows = invoke("--group", "profile")
    assert result.returncode == 0, result.stderr
    actions = operations(rows)
    assert actions[0] == ["stop", "profile-runner-worker"]
    assert actions[1] == ["stop", "profile-worker"]
    assert actions[2][-2:] == ["api", "profile-worker"]
    assert actions[3][-1] == "profile-runner-worker"
    assert len(actions) == 4


def test_failed_execution_health_keeps_dispatch_paused(release_project):
    project, _, _, invoke = release_project
    result, rows = invoke("--group", "profile", failure=True)
    assert result.returncode != 0
    actions = operations(rows)
    assert len(actions) == 3
    assert actions[-1][-2:] == ["api", "profile-worker"]
    record = json.loads(next((project / ".releases").glob("*.json")).read_text())
    assert record["status"] == "failed"
    assert record["stage"] == "update_execution"


def test_check_only_validates_configuration(release_project):
    project, _, _, invoke = release_project
    result, rows = invoke("--check")
    assert result.returncode == 0, result.stderr
    assert operations(rows) == []
    assert not (project / ".releases").exists()


def test_invalid_component_configuration_is_rejected_before_pause(release_project):
    project, _, _, invoke = release_project
    result, rows = invoke(preflight_failure=True)
    assert result.returncode != 0
    assert operations(rows) == []
    assert not (project / ".releases").exists()


def test_active_legacy_execution_worker_blocks_the_cutover(release_project):
    project, _, _, invoke = release_project
    result, rows = invoke(legacy_worker=True)
    assert result.returncode != 0
    assert "legacy shared workflow worker" in result.stderr
    assert operations(rows) == []
    assert not (project / ".releases").exists()


def test_invitation_group_requires_its_dispatcher_profile(release_project):
    _, _, _, invoke = release_project
    result, rows = invoke("--group", "invitation")
    assert result.returncode != 0
    assert "--profile invitation-runner" in result.stderr
    assert operations(rows) == []


def test_mutable_image_is_rejected_before_docker_calls(release_project):
    _, _, _, invoke = release_project
    result, rows = invoke("--image", "example/customer-profile:latest")
    assert result.returncode != 0
    assert rows == []


def test_untracked_source_is_rejected_before_release(release_project):
    project, _, _, invoke = release_project
    (project / "src").mkdir()
    (project / "src/uncommitted.py").write_text("VALUE = 1\n", encoding="utf-8")
    result, rows = invoke(build=True)
    assert result.returncode != 0
    assert "untracked build inputs" in result.stderr
    assert operations(rows) == []
    assert not any(row["args"][0] == "build" for row in rows)


def test_releases_to_the_same_docker_project_are_serialized(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("release_lock_test", ROOT / "scripts/release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    with module.deployment_lock("engine", "project"):
        with pytest.raises(ValueError, match="Another release"):
            with module.deployment_lock("engine", "project"):
                pytest.fail("parallel release acquired the same lock")
        with module.deployment_lock("engine", "another-project"):
            pass
    with module.deployment_lock("engine", "project"):
        pass

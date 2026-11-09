#!/usr/bin/env python3
"""Build one versioned image and update dispatch/execution services in order."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = {
    "profile": ["api", "profile-worker"],
    "invitation": ["inbound", "invitation-worker"],
}
DISPATCHERS = [
    "qingtian-worker", "commercial-worker", "profile-runner-worker", "invitation-runner-worker",
]


def run(command: list[str], env: dict[str, str], *, capture: bool = False) -> str:
    result = subprocess.run(
        command, cwd=ROOT, env=env, check=True, text=True, capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def validate_image(image: str, *, existing: bool) -> None:
    if existing and re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", image):
        return
    match = re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*:([A-Za-z0-9][A-Za-z0-9_.-]*)", image)
    stamp = re.search(r"(?:^|[_.-])(\d{8})[-_T]?(\d{6})(?:$|[_.-])", match[1]) if match else None
    if stamp is None:
        raise ValueError("Image tag must contain a full local timestamp, such as 20261109-143000.")
    datetime.strptime(stamp[1] + stamp[2], "%Y%m%d%H%M%S")


def save_record(path: Path, record: dict) -> None:
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


@contextmanager
def deployment_lock(engine: str, project: str):
    """Serialize releases to the same Docker project across local checkouts."""
    identity = hashlib.sha256(f"{engine}/{project}".encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"customer-profile-release-{identity}.lock"
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another release is active for this Docker project.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def release(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    revision = run(["git", "rev-parse", "HEAD"], env, capture=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    image = args.image or (env.get("CP_IMAGE") if args.skip_build else None)
    if args.skip_build and not image:
        raise ValueError("--skip-build requires --image or CP_IMAGE.")
    image = image or f"customer-profile-unified:{stamp}-{revision[:12]}"
    validate_image(image, existing=args.skip_build)
    env["CP_IMAGE"] = image

    compose = [str(ROOT / "scripts/compose.sh")]
    if args.env_file:
        compose.extend(["--env-file", str(Path(args.env_file).resolve())])
    if args.task_env_file:
        compose.extend(["--task-env-file", str(Path(args.task_env_file).resolve())])
    for profile in args.profile:
        compose.extend(["--profile", profile])
    configuration = json.loads(run(compose + ["config", "--format", "json"], env, capture=True))
    active = set(configuration["services"])
    images = {service["image"] for service in configuration["services"].values()}
    project = configuration["name"]
    del configuration
    if images != {image}:
        raise ValueError("All Compose services must reference the selected unified image.")
    if args.group == "invitation" and "invitation-runner-worker" not in active:
        raise ValueError("Invitation release requires --profile invitation-runner.")
    if args.migrate and args.group not in {"all", "backend"}:
        raise ValueError("--migrate is available for the all/backend release groups.")

    runtime = []
    if args.group == "all":
        runtime = RUNTIME["profile"] + RUNTIME["invitation"]
    elif args.group in RUNTIME:
        runtime = RUNTIME[args.group]
    dispatchers = [name for name in DISPATCHERS if name in active]
    if args.group == "profile":
        dispatchers = ["profile-runner-worker"]
    elif args.group == "invitation":
        dispatchers = ["invitation-runner-worker"]
    backend_api = ["task-api"] if args.group in {"all", "backend"} else []
    selected = runtime + backend_api + dispatchers
    missing = set(selected) - active
    if missing:
        raise ValueError("Missing Compose services: " + ", ".join(sorted(missing)))
    print(f"Image {image}\nGroup {args.group}\nServices {', '.join(selected)}", flush=True)
    if args.check:
        return 0

    engine = run(["docker", "info", "--format", "{{.ID}}"], env, capture=True)
    with deployment_lock(engine, project):
        if not args.skip_build:
            dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], env, capture=True)
            if dirty:
                raise ValueError("Commit tracked changes before building a release image.")
            untracked = run([
                "git", "ls-files", "--others", "--exclude-standard", "--",
                "Dockerfile", ".dockerignore", "requirements.txt", "src", "frontend/dist",
                "scripts/container-entrypoint.sh",
                "tests/fixtures/replay/human_corrected_info.json",
                "services/task-backend/pyproject.toml", "services/task-backend/src",
                "services/task-backend/contracts", "services/task-backend/db",
            ], env, capture=True)
            if untracked:
                raise ValueError("Commit untracked build inputs before building a release image.")
            existing = run(
                ["docker", "image", "ls", "--quiet", "--filter", f"reference={image}"],
                env, capture=True,
            )
            if existing:
                raise ValueError("The selected image tag already exists locally. Choose a new timestamp.")
            run([
                "docker", "build", "--tag", image,
                "--build-arg", f"BUILD_REVISION={revision}", str(ROOT),
            ], env)

        labels = json.loads(run([
            "docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image,
        ], env, capture=True)) or {}
        if labels.get("io.customer-profile.layout") != "v1":
            raise ValueError("The selected image must provide the unified v1 layout.")

        # Parse settings in isolated containers before stopping any running service.
        # The exception hook keeps configuration values out of deployment output.
        validation_header = (
            "import sys; sys.excepthook = lambda kind, value, tb: "
            "print('Configuration validation failed (' + kind.__name__ + ')', file=sys.stderr); "
        )
        for service in selected:
            if service in runtime:
                validation = "from customer_profile.settings import get_settings; get_settings()"
            else:
                component = "api" if service == "task-api" else "worker"
                validation = f"from src.config.settings import load_config; load_config(component={component!r})"
            print(f"Validate configuration for {service}", flush=True)
            run(compose + [
                "run", "--rm", "--no-deps", "--entrypoint", "python", service,
                "-c", validation_header + validation,
            ], env)

        previous_output = run(compose + ["ps", "--all", "--format", "json"], env, capture=True)
        previous = []
        if previous_output:
            try:
                rows = json.loads(previous_output)
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in previous_output.splitlines()]
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if row["Service"] == "worker" and row["State"] in {"running", "restarting", "paused"}:
                    raise ValueError("Retire the legacy shared workflow worker before releasing dedicated workers.")
                if row["Service"] in selected:
                    image_id = run(["docker", "inspect", "--format", "{{.Image}}", row["ID"]], env, capture=True)
                    previous.append({
                        "service": row["Service"], "container": row["ID"],
                        "image": row["Image"], "image_id": image_id, "state": row["State"],
                    })
        history = ROOT / ".releases"
        history.mkdir(exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=stamp + "-", suffix=".json", dir=history)
        os.close(descriptor)
        record_path = Path(filename)
        record = {
            "project": project, "engine": engine,
            "image": image, "revision": labels.get("org.opencontainers.image.revision"),
            "release_source_revision": revision, "group": args.group,
            "profiles": args.profile, "services": selected, "previous": previous,
            "status": "running", "stage": "pause_dispatch",
        }
        save_record(record_path, record)
        print(f"Release record {record_path}", flush=True)
        try:
            run(compose + ["stop", *dispatchers], env)
            record["stage"] = "drain_execution"
            save_record(record_path, record)
            execution_workers = [name for name in runtime if name.endswith("-worker")]
            if execution_workers:
                run(compose + ["stop", *execution_workers], env)
            if args.migrate:
                record["stage"] = "migrate"
                save_record(record_path, record)
                run(compose + ["run", "--rm", "--no-deps", "task-migrate"], env)
            record["stage"] = "update_execution"
            save_record(record_path, record)
            up = ["up", "-d", "--no-build", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", str(args.wait_timeout)]
            if runtime:
                run(compose + up + runtime, env)
            record["stage"] = "update_task_api"
            save_record(record_path, record)
            if backend_api:
                run(compose + up + backend_api, env)
            record["stage"] = "resume_dispatch"
            save_record(record_path, record)
            run(compose + up + dispatchers, env)
            record.update(status="succeeded", stage="complete")
            save_record(record_path, record)
            print(f"Released {image}", flush=True)
        except (subprocess.CalledProcessError, KeyboardInterrupt):
            record["status"] = "failed"
            save_record(record_path, record)
            print(f"Release stopped during {record['stage']}. Inspect {record_path} before resuming dispatch.", file=sys.stderr)
            raise
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Execution-service environment file")
    parser.add_argument("--task-env-file", help="Task-backend environment file")
    parser.add_argument("--image", help="Versioned image tag or existing image digest")
    parser.add_argument("--group", choices=["all", "backend", "profile", "invitation"], default="all")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--skip-build", action="store_true", help="Deploy an existing local image")
    parser.add_argument("--migrate", action="store_true", help="Run MySQL migrations explicitly")
    parser.add_argument("--check", action="store_true", help="Validate configuration and show the release scope")
    parser.add_argument("--wait-timeout", type=int, default=120)
    args = parser.parse_args()
    if args.wait_timeout < 1:
        parser.error("--wait-timeout must be positive")
    try:
        return release(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

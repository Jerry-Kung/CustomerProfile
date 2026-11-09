#!/usr/bin/env python3
"""Generate private Helm values from exported production Kubernetes YAML.

The output contains credentials and is intentionally gitignored. The exporter
validates the local/commercial QingTian key relationships before writing so a
stale migration Job cannot accidentally become the production source of truth.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import stat
from typing import Any

import yaml


RESOURCE_FILES = {
    "api": "customer-profile-backend.aizt01-pro.deployment.yaml",
    "service": "customer-profile-backend.aizt01-pro.service.yaml",
    "logs_pvc": "customer-profile-logs.aizt01-pro.persistentvolumeclaim.yaml",
    "migrate": "customer-profile-migrate.aizt01-pro.job.yaml",
    "uploads_pvc": "customer-profile-task-uploads.aizt01-pro.persistentvolumeclaim.yaml",
    "commercial": "customer-profile-worker-secondary.aizt01-pro.deployment.yaml",
    "local": "customer-profile-worker.aizt01-pro.deployment.yaml",
}


def _load_resource(root: Path, key: str) -> dict[str, Any]:
    path = root / RESOURCE_FILES[key]
    if not path.is_file():
        raise ValueError(f"missing production manifest: {path}")
    documents = [item for item in yaml.safe_load_all(path.read_text()) if item]
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError(f"expected exactly one Kubernetes resource in {path}")
    return documents[0]


def _pod_spec(resource: dict[str, Any]) -> dict[str, Any]:
    return resource["spec"]["template"]["spec"]


def _container(resource: dict[str, Any]) -> dict[str, Any]:
    containers = _pod_spec(resource).get("containers") or []
    if len(containers) != 1:
        raise ValueError(
            f"{resource.get('kind')}/{resource.get('metadata', {}).get('name')} "
            "must contain exactly one container"
        )
    return containers[0]


def _env(resource: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _container(resource).get("env") or []:
        name = item.get("name")
        if name and "value" in item:
            result[str(name)] = str(item.get("value", ""))
    return result


def _required(mapping: dict[str, str], key: str, owner: str) -> str:
    value = mapping.get(key, "").strip()
    if not value:
        raise ValueError(f"{owner} is missing required environment variable {key}")
    return value


def _assert_same(label: str, values: list[str]) -> str:
    if not values or len(set(values)) != 1:
        raise ValueError(f"production manifests disagree on {label}")
    return values[0]


def _resource_name(resource: dict[str, Any]) -> str:
    return str(resource["metadata"]["name"])


def _image_parts(resource: dict[str, Any]) -> tuple[str, str]:
    image = str(_container(resource)["image"])
    if ":" not in image:
        raise ValueError(f"container image must include a tag: {image}")
    return tuple(image.rsplit(":", 1))  # type: ignore[return-value]


def _pvc_values(resource: dict[str, Any]) -> dict[str, Any]:
    spec = resource["spec"]
    access_modes = spec.get("accessModes") or []
    size = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    if len(access_modes) != 1 or not size:
        raise ValueError(f"invalid PVC {_resource_name(resource)}")
    return {
        "enabled": True,
        "existingClaim": _resource_name(resource),
        "accessMode": str(access_modes[0]),
        "size": str(size),
        "storageClassName": str(spec.get("storageClassName") or ""),
        "keep": True,
    }


def _resources(resource: dict[str, Any]) -> dict[str, Any]:
    return dict(_container(resource).get("resources") or {})


def generate_values(source_dir: Path, *, image_tag: str | None = None) -> dict[str, Any]:
    resources = {key: _load_resource(source_dir, key) for key in RESOURCE_FILES}
    api_env = _env(resources["api"])
    local_env = _env(resources["local"])
    commercial_env = _env(resources["commercial"])
    migrate_env = _env(resources["migrate"])

    mysql_host = _assert_same(
        "MYSQL_HOST",
        [_required(env, "MYSQL_HOST", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
            ("migration", migrate_env),
        )],
    )
    mysql_port = _assert_same(
        "MYSQL_PORT",
        [_required(env, "MYSQL_PORT", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
            ("migration", migrate_env),
        )],
    )
    mysql_database = _assert_same(
        "MYSQL_DATABASE",
        [_required(env, "MYSQL_DATABASE", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
            ("migration", migrate_env),
        )],
    )
    mysql_user = _assert_same(
        "MYSQL_USER",
        [_required(env, "MYSQL_USER", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
            ("migration", migrate_env),
        )],
    )
    mysql_password = _assert_same(
        "MYSQL_PASSWORD",
        [_required(env, "MYSQL_PASSWORD", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
            ("migration", migrate_env),
        )],
    )

    qingtian_base_url = _assert_same(
        "QINGTIAN_BASE_URL",
        [_required(env, "QINGTIAN_BASE_URL", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
        )],
    )
    qingtian_user_id = _assert_same(
        "QINGTIAN_USER_ID",
        [_required(env, "QINGTIAN_USER_ID", name) for name, env in (
            ("api", api_env),
            ("local worker", local_env),
            ("commercial worker", commercial_env),
        )],
    )
    local_test_key = _assert_same(
        "local QINGTIAN_API_KEY_TEST",
        [
            _required(api_env, "QINGTIAN_API_KEY_TEST", "api"),
            _required(local_env, "QINGTIAN_API_KEY_TEST", "local worker"),
        ],
    )
    local_production_key = _assert_same(
        "local QINGTIAN_API_KEY_PRODUCTION",
        [
            _required(api_env, "QINGTIAN_API_KEY_PRODUCTION", "api"),
            _required(local_env, "QINGTIAN_API_KEY_PRODUCTION", "local worker"),
        ],
    )
    invitation_key = _assert_same(
        "shared QINGTIAN_INVITATION_STRATEGY_API_KEY",
        [
            _required(api_env, "QINGTIAN_INVITATION_STRATEGY_API_KEY", "api"),
            _required(local_env, "QINGTIAN_INVITATION_STRATEGY_API_KEY", "local worker"),
            _required(
                commercial_env,
                "QINGTIAN_INVITATION_STRATEGY_API_KEY",
                "commercial worker",
            ),
        ],
    )

    image_repository, detected_tag = _image_parts(resources["api"])
    for component in ("local", "commercial", "migrate"):
        repository, tag = _image_parts(resources[component])
        if repository != image_repository or tag != detected_tag:
            raise ValueError("production workloads do not use one common image")

    service_spec = resources["service"]["spec"]
    service_ports = service_spec.get("ports") or []
    if len(service_ports) != 1:
        raise ValueError("production Service must expose exactly one port")

    api_container = _container(resources["api"])
    local_container = _container(resources["local"])
    commercial_container = _container(resources["commercial"])
    migrate_container = _container(resources["migrate"])
    api_port = int(_required(api_env, "API_PORT", "api"))

    return {
        "names": {
            "configMap": "customer-profile-backend-config",
            "commonSecret": "customer-profile-backend-common-secret",
            "localSecret": "customer-profile-backend-local-secret",
            "commercialSecret": "customer-profile-backend-commercial-secret",
            "logsPvc": _resource_name(resources["logs_pvc"]),
            "taskUploadsPvc": _resource_name(resources["uploads_pvc"]),
            "api": _resource_name(resources["api"]),
            "service": _resource_name(resources["service"]),
            "localWorker": _resource_name(resources["local"]),
            "commercialWorker": _resource_name(resources["commercial"]),
            "migrate": _resource_name(resources["migrate"]),
        },
        "image": {
            "repository": image_repository,
            "tag": image_tag or detected_tag,
            "pullPolicy": "Always",
        },
        "config": {
            "APP_ENV": _required(api_env, "APP_ENV", "api"),
            "LOG_LEVEL": _required(api_env, "LOG_LEVEL", "api"),
            "LOG_DIR": _required(api_env, "LOG_DIR", "api"),
            "API_HOST": _required(api_env, "API_HOST", "api"),
            "API_PORT": str(api_port),
            "TASK_UPLOAD_DIR": _required(api_env, "TASK_UPLOAD_DIR", "api"),
            "WORKFLOW_PLATFORM": "qingtian",
            "QINGTIAN_BASE_URL": qingtian_base_url,
            "QINGTIAN_USER_ID": qingtian_user_id,
            "QINGTIAN_POLL_INTERVAL_MS": _assert_same(
                "QINGTIAN_POLL_INTERVAL_MS",
                [
                    _required(api_env, "QINGTIAN_POLL_INTERVAL_MS", "api"),
                    _required(local_env, "QINGTIAN_POLL_INTERVAL_MS", "local worker"),
                    _required(
                        commercial_env,
                        "QINGTIAN_POLL_INTERVAL_MS",
                        "commercial worker",
                    ),
                ],
            ),
            "QINGTIAN_POLL_TIMEOUT_MS": _assert_same(
                "QINGTIAN_POLL_TIMEOUT_MS",
                [
                    _required(api_env, "QINGTIAN_POLL_TIMEOUT_MS", "api"),
                    _required(local_env, "QINGTIAN_POLL_TIMEOUT_MS", "local worker"),
                    _required(
                        commercial_env,
                        "QINGTIAN_POLL_TIMEOUT_MS",
                        "commercial worker",
                    ),
                ],
            ),
            "WORKER_POLL_INTERVAL_MS": "2000",
            "TASK_QUEUE_MAX_WAITING": "500",
            "MYSQL_HOST": mysql_host,
            "MYSQL_PORT": mysql_port,
            "MYSQL_DATABASE": mysql_database,
            "MIGRATION_DB_WAIT_MAX_TRIES": "30",
            "MIGRATION_DB_WAIT_INTERVAL_MS": "2000",
        },
        "secrets": {
            "common": {
                "MYSQL_USER": mysql_user,
                "MYSQL_PASSWORD": mysql_password,
                "QINGTIAN_INVITATION_STRATEGY_API_KEY": invitation_key,
            },
            "local": {
                "QINGTIAN_API_KEY_TEST": local_test_key,
                "QINGTIAN_API_KEY_PRODUCTION": local_production_key,
            },
            "commercial": {
                "QINGTIAN_API_KEY_TEST": _required(
                    commercial_env,
                    "QINGTIAN_API_KEY_TEST",
                    "commercial worker",
                ),
                "QINGTIAN_API_KEY_PRODUCTION": _required(
                    commercial_env,
                    "QINGTIAN_API_KEY_PRODUCTION",
                    "commercial worker",
                ),
            },
        },
        "persistence": {
            "logs": _pvc_values(resources["logs_pvc"]),
            "taskUploads": _pvc_values(resources["uploads_pvc"]),
        },
        "retry": {
            "policyVersion": "local_first_v1",
            "maxWorkflowAttempts": 3,
            "maxPollRecoveries": 3,
            "maxRunLifetimeSeconds": 7200,
        },
        "api": {
            "replicas": int(resources["api"]["spec"].get("replicas") or 1),
            "imagePullPolicy": str(api_container.get("imagePullPolicy") or "IfNotPresent"),
            "port": api_port,
            "terminationGracePeriodSeconds": int(
                _pod_spec(resources["api"]).get("terminationGracePeriodSeconds") or 30
            ),
            "resources": _resources(resources["api"]),
        },
        "localWorker": {
            "replicas": int(resources["local"]["spec"].get("replicas") or 1),
            "imagePullPolicy": str(local_container.get("imagePullPolicy") or "Always"),
            "concurrency": int(_required(local_env, "WORKER_CONCURRENCY", "local worker")),
            "terminationGracePeriodSeconds": int(
                _pod_spec(resources["local"]).get("terminationGracePeriodSeconds") or 60
            ),
            "resources": _resources(resources["local"]),
        },
        "commercialWorker": {
            "replicas": int(resources["commercial"]["spec"].get("replicas") or 1),
            "imagePullPolicy": str(
                commercial_container.get("imagePullPolicy") or "Always"
            ),
            "concurrency": int(
                _required(
                    commercial_env,
                    "WORKER_CONCURRENCY",
                    "commercial worker",
                )
            ),
            "queueThreshold": 5,
            "terminationGracePeriodSeconds": int(
                _pod_spec(resources["commercial"]).get(
                    "terminationGracePeriodSeconds"
                )
                or 60
            ),
            "mountTaskUploads": False,
            "resources": _resources(resources["commercial"]),
        },
        "service": {
            "type": str(service_spec.get("type") or "ClusterIP"),
            "port": int(service_ports[0]["port"]),
        },
        "migration": {
            "enabled": True,
            "imagePullPolicy": str(
                migrate_container.get("imagePullPolicy") or "IfNotPresent"
            ),
            "backoffLimit": int(resources["migrate"]["spec"].get("backoffLimit") or 0),
            "terminationGracePeriodSeconds": int(
                _pod_spec(resources["migrate"]).get("terminationGracePeriodSeconds") or 30
            ),
            "resources": _resources(resources["migrate"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "deploy/helm/customer-profile-backend/values.production.private.yaml"
        ),
    )
    parser.add_argument(
        "--image-tag",
        help="Override the image tag exported from production YAML.",
    )
    args = parser.parse_args()

    values = generate_values(args.source_dir, image_tag=args.image_tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(values, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    args.output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(
        f"generated private production values: {args.output} "
        "(mode 0600; secrets not printed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

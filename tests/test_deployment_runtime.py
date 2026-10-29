"""部署配置必须影响真实服务装配，独立 worker 只创建一套执行服务。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from customer_profile import worker as worker_module
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.settings import Settings

from .conftest import make_settings


@pytest.mark.parametrize("replicas", [1, 2])
async def test_service_applies_quota_and_sqlite_settings(tmp_path, replicas):
    settings = make_settings(
        tmp_path,
        _env_file=None,
        inline_worker=False,
        worker_replicas=replicas,
        llm_rpm_limit=60,
        llm_tpm_limit=6000,
        llm_rpm_burst=None,
        llm_tpm_burst=None,
        sqlite_busy_timeout_ms=1234,
    )
    service = await build_service(settings, replay=ReplaySource(strict=True))
    try:
        snapshot = service.quota.snapshot()
        assert snapshot["rpm_capacity"] == 60 / replicas
        assert snapshot["tpm_capacity"] == 6000 / replicas
        assert settings.llm_rpm_limit == 60
        assert settings.llm_tpm_limit == 6000
        rows = await service.store._read("PRAGMA busy_timeout")
        assert rows[0]["timeout"] == 1234
    finally:
        await service.aclose()
        service.store.close()


async def test_standalone_worker_does_not_create_an_inline_consumer(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path, _env_file=None, inline_worker=True)
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "_install_signal_handlers", lambda *args: None)
    consumers = []

    async def inspect_consumer(worker):
        consumers.append(worker)
        assert worker.service.inline_worker_task is None
        assert worker.settings.inline_worker is False

    monkeypatch.setattr(worker_module.Worker, "run_forever", inspect_consumer)
    assert await worker_module.main_async() == 0
    assert len(consumers) == 1
    assert settings.inline_worker is True
    assert not consumers[0].service.store.connected


@pytest.mark.parametrize("obsolete_concurrency", [1, 20])
def test_compose_follows_backend_dispatch_and_names_images_automatically(
    tmp_path, obsolete_concurrency
):
    if not shutil.which("docker"):
        pytest.skip("需要 Docker Compose CLI，不需要 daemon")
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / "worker env"
    env_file.write_text(
        # 旧配置即使还在 env 中，也不能继续构成第二套业务限制或覆盖构建命名。
        f"PROFILE_WORKER_CONCURRENCY={obsolete_concurrency}\nINVITATION_WORKER_CONCURRENCY=5\n"
        "PROFILE_IMAGE=obsolete-profile\nINVITATION_IMAGE=obsolete-invitation\n"
        "LLM_RPM_LIMIT=100\nLLM_TPM_LIMIT=2000\n",
        encoding="utf-8",
    )
    runtime = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("COMPOSE_", "CP_", "WORKER_", "PROFILE_", "INVITATION_", "LLM_")
        )
    }
    runtime["CP_ENV_FILE"] = str(env_file)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(root / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=runtime,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "Compose 配置渲染失败（不输出凭据）"
    services = json.loads(result.stdout)["services"]
    assert "worker" not in services, "旧的全任务消费者不能继续作为默认服务启动"
    for name, scope in (
        ("profile-worker", "profile"),
        ("invitation-worker", "invitation"),
    ):
        settings = Settings(
            _env_file=None,
            **{
                key.lower(): value
                for key, value in services[name]["environment"].items()
            },
        )
        assert settings.workflow_scope == scope
        assert settings.worker_concurrency is None
        assert settings.worker_replicas == 2
        assert settings.per_process_rpm_limit == 50
        assert settings.per_process_tpm_limit == 1000
        assert not settings.inline_worker
        assert settings.interrupt_on_start
        assert "depends_on" not in services[name]
        assert "ports" not in services[name]
    assert all("image" not in service for service in services.values())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(root / "compose.yaml"),
            "config",
            "--images",
        ],
        env=runtime,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "自动镜像命名校验失败（不输出凭据）"
    images = {line.split(":")[0] for line in result.stdout.splitlines() if line}
    assert images == {"customer-profile-" + name for name in services}
    for name in ("api", "inbound"):
        assert services[name]["environment"]["INTERRUPT_ON_START"] == "false"
        assert services[name]["environment"]["INLINE_WORKER"] == "false"
    assert services["api"]["environment"]["WORKFLOW_SCOPE"] == "profile"
    assert services["inbound"]["environment"]["WORKFLOW_SCOPE"] == "invitation"


async def test_worker_entrypoint_recovers_only_its_scope_and_only_once(
    tmp_path, monkeypatch
):
    from customer_profile.persistence import Store

    settings = make_settings(
        tmp_path,
        _env_file=None,
        workflow_scope="profile",
        worker_concurrency=5,
        interrupt_on_start=True,
    )
    calls = []
    recover = Store.mark_running_as_interrupted

    async def observe_recovery(store, **kwargs):
        calls.append(kwargs["workflow_scope"])
        return await recover(store, **kwargs)

    async def consume(worker):
        worker.request_stop()

    monkeypatch.setattr(Store, "mark_running_as_interrupted", observe_recovery)
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "_install_signal_handlers", lambda *_: None)
    monkeypatch.setattr(worker_module.Worker, "_consume", consume)
    assert await worker_module.main_async() == 0
    assert calls == ["profile"]

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


@pytest.mark.parametrize("concurrency", [1, 10])
def test_compose_configures_slots_in_one_worker_process(tmp_path, concurrency):
    if not shutil.which("docker"):
        pytest.skip("需要 Docker Compose CLI，不需要 daemon")
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / "worker env"
    env_file.write_text(f"WORKER_CONCURRENCY={concurrency}\n", encoding="utf-8")
    runtime = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "CP_", "WORKER_"))
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
    env = services["worker"]["environment"]
    settings = Settings(
        _env_file=None, **{key.lower(): value for key, value in env.items()}
    )
    assert settings.worker_concurrency == concurrency
    assert settings.worker_replicas == 1
    assert not settings.inline_worker
    assert settings.interrupt_on_start
    for name in ("api", "inbound"):
        assert services[name]["environment"]["INTERRUPT_ON_START"] == "false"

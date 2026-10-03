"""部署配置必须影响真实服务装配，且独立 worker 只创建一个消费者。"""

from __future__ import annotations

import pytest

from customer_profile import worker as worker_module
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service

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


async def test_standalone_worker_does_not_create_an_inline_consumer(tmp_path, monkeypatch):
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

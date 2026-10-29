"""Opt-in cross-repository test using real native routes, SQLite and mock upstreams.

Run with CustomerProfile's development Python (its runtime dependencies are needed),
RUN_NATIVE_INVITATION_CONTRACT=1 and CUSTOMERPROFILE_SOURCE_ROOT set to that checkout.
No network connection, production configuration or MySQL database is used.
"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from src.config.settings import InvitationRunnerConfig
from src.infrastructure.invitation_runner.client import InvitationRunnerClient

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_NATIVE_INVITATION_CONTRACT") != "1",
    reason="requires explicitly selected CustomerProfile source checkout",
)


@pytest.mark.parametrize("file_count", [0, 2])
def test_backend_adapter_against_real_customerprofile_inbound_and_worker(
    tmp_path, monkeypatch, file_count
):
    root = Path(os.environ["CUSTOMERPROFILE_SOURCE_ROOT"]).resolve()
    monkeypatch.syspath_prepend(str(root / "src"))
    from customer_profile.inbound import create_inbound_app
    from customer_profile.runner import build_service
    from customer_profile.settings import Settings
    from customer_profile.worker import Worker

    spec = importlib.util.spec_from_file_location(
        "native_contract_fixture", root / "tests/test_invitation.py"
    )
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)

    async def scenario():
        urls = [f"https://audio.invalid/file-{i}.wav" for i in range(file_count)]
        upstream = fixtures.OfflineServices([fixtures.recording(*urls)])
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'native.db'}",
            inbound_api_key="offline-inbound",
            inline_worker=False,
            interrupt_on_start=False,
            writeback_enabled=True,
            llm_base_url="https://llm.invalid/v1",
            llm_model="offline",
            llm_api_key="offline-model",
            profile_api_base_url="https://test-m2m.invalid",
            profile_api_key="offline-business",
            auc_base_url="https://asr.invalid/api/v3/auc/bigmodel",
            auc_api_key="offline-asr",
            auc_poll_interval_s=0,
            http_retry_enabled=False,
            replay_mode="off",
        )
        service = await build_service(settings)
        service.http_client._transport = httpx.MockTransport(upstream.handle)
        service.llm_client._transport = httpx.MockTransport(upstream.handle)
        service.llm_client._backoff_seconds = lambda _: 0

        async def factory():
            return service

        app = create_inbound_app(service_factory=factory)
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()
        requests_seen = []

        async def consume():
            worker = Worker(service, settings)
            while not stopped.is_set():
                if not await worker.run_once():
                    await asyncio.sleep(0.005)

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://native.invalid"
            ) as native:

                class SyncBridge(httpx.BaseTransport):
                    def handle_request(self, request):
                        requests_seen.append(request)

                        async def call():
                            response = await native.request(
                                request.method,
                                str(request.url),
                                headers=request.headers,
                                content=request.content,
                            )
                            return httpx.Response(
                                response.status_code,
                                headers=response.headers,
                                content=response.content,
                                request=request,
                            )

                        return asyncio.run_coroutine_threadsafe(call(), loop).result(
                            timeout=10
                        )

                client = InvitationRunnerClient(
                    InvitationRunnerConfig(
                        base_url_test="http://native.invalid",
                        api_key_test="offline-inbound",
                        poll_interval_ms=5,
                        poll_timeout_ms=10000,
                    ),
                    SyncBridge(),
                )
                consumer = asyncio.create_task(consume())
                try:
                    result = json.loads(
                        await asyncio.to_thread(
                            client.run_workflow,
                            customer_phonenumber=fixtures.PHONE,
                            batch_id=fixtures.BATCH,
                            execution_id=fixtures.EXECUTION,
                            work_env="test",
                        )
                    )
                    assert result["execution_id"] == fixtures.EXECUTION
                    assert result == upstream.callbacks[0]
                    assert upstream.asr_urls == urls
                    assert len([r for r in requests_seen if r.method == "POST"]) == 1
                    assert all(
                        r.headers["X-API-Key"] == "offline-inbound"
                        for r in requests_seen
                    )
                finally:
                    stopped.set()
                    await asyncio.wait_for(consumer, timeout=10)

    asyncio.run(scenario())

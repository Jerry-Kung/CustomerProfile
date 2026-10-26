"""整合回归：真实业务管线经过接单、持久化队列、worker 和 v2 回写。

只替换外部 HTTP transport；工作流、模板、调度、子运行与留痕均使用真实实现。
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest

from customer_profile import runner
from customer_profile.api import DEFAULT_BATCH_ID, create_app
from customer_profile.inbound import create_inbound_app
from customer_profile.worker import Worker
from customer_profile.workflows import customer_profile_pipeline as pipeline

from .conftest import make_settings


@pytest.mark.parametrize(
    ("via", "batch_id", "writeback_enabled"),
    [
        ("api", None, False),
        ("api", "   ", False),
        ("api", "batch-api", True),
        ("inbound", "batch-inbound", False),
        ("inbound", "batch-inbound", True),
    ],
)
async def test_submission_runs_real_pipeline_and_preserves_v2_callback(
    tmp_path, monkeypatch, via, batch_id, writeback_enabled
):
    callbacks: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/data/history/" in request.url.path:
            return httpx.Response(200, json=[])
        if request.method == "GET" and "/data/profile/" in request.url.path:
            return httpx.Response(200, json={"basic_notes": {}})
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {"content": json.dumps({
                            "facts": [], "fields": [], "claims": [],
                        })},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/callback/update-profile"):
            assert request.headers["X-API-Key"] == "test-profile-key"
            callbacks.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"未预期的外部请求：{request.method} {request.url.path}")

    monkeypatch.setattr(runner, "_build_transport", lambda replay: httpx.MockTransport(handle))
    settings = make_settings(
        tmp_path,
        _env_file=None,
        inline_worker=False,
        replay_mode="off",
        writeback_enabled=writeback_enabled,
        inbound_api_key="test-inbound-key",
        profile_api_key="test-profile-key",
        llm_base_url="http://offline-llm/v1",
        profile_api_base_url="http://offline-profile",
        mhero_base_url="http://offline-mhero",
        http_retry_enabled=False,
        llm_retry_enabled=False,
    )
    service = await runner.build_service(settings)

    async def factory():
        return service

    app = (
        create_app(service_factory=factory)
        if via == "api"
        else create_inbound_app(service_factory=factory)
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            inputs = {"phone_number": "13800000000"}
            if batch_id is not None:
                inputs["batch_id"] = batch_id
            response = await client.post(
                "/runs" if via == "api" else "/api/v1/tasks",
                json={"workflow_id": pipeline.WORKFLOW_ID, "inputs": inputs}
                if via == "api" else inputs,
                headers={"X-API-Key": "test-inbound-key"},
            )
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            queued = await service.store.get_run(run_id)
            expected_batch = batch_id if batch_id and batch_id.strip() else DEFAULT_BATCH_ID
            assert queued["status"] == "queued"
            assert queued["inputs_json"]["batch_id"] == expected_batch

            assert await Worker(service, settings).run_once()
            detail = await service.store.get_run(run_id)
            assert detail["status"] == "succeeded", detail["error"]
            assert isinstance(detail["outputs_json"]["result1"], str)
            payload = json.loads(detail["outputs_json"]["result1"])
            assert set(payload) == {"phone", "batch_id", "analysis_result"}
            assert payload["phone"] == "13800000000"
            assert payload["batch_id"] == expected_batch
            assert isinstance(payload["analysis_result"], dict)
            assert {"profile_summary", "basic_notes_updates"} <= payload["analysis_result"].keys()
            assert payload["analysis_result"].keys() <= {
                "profile_summary", "basic_notes_updates", "ai_insight",
                "purchase_decisions", "deal_battle", "decision_psychology",
            }
            nodes = await service.store.list_node_executions(run_id)
            assert not [node for node in nodes if node["status"] == "running"]
            builder = next(node for node in nodes if node["node_id"] == pipeline.CALLBACK_BUILD)
            assert json.loads(builder["outputs_json"]["callback_json"]) == payload
            writeback = next(node for node in nodes if node["node_id"] == pipeline.CALLBACK_WRITE)
            if writeback_enabled:
                assert callbacks == [payload]
                assert writeback["outputs_json"]["status"] == 200
            else:
                assert callbacks == []
                assert json.loads(writeback["outputs_json"]["body"])["writeback_skipped"] is True


@pytest.mark.parametrize(
    ("module_name", "host_key", "port_key"),
    [("main", "api_host", "api_port"), ("inbound", "inbound_host", "inbound_port")],
)
def test_entrypoint_keeps_configurable_listener_and_shared_logging(
    tmp_path, monkeypatch, module_name, host_key, port_key
):
    import uvicorn

    module = importlib.import_module(f"customer_profile.{module_name}")
    settings = make_settings(
        tmp_path, _env_file=None,
        **{host_key: "127.0.0.1", port_key: 19273},
    )
    calls = []
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    module.main()
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 19273
    assert calls[0]["log_config"] is None

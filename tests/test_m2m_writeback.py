"""真实合并、回写和出口节点对接 M2M JSON 契约；HTTP 仅走本地 ASGI。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from customer_profile.definitions import Binding, NodeStatus, RunStatus, WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.settings import Settings
from customer_profile.workflows import customer_profile_production as production

INPUT = "m2m_input"
API_KEY = "offline-m2m-test-key"


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str
    batch_id: str
    analysis_result: dict[str, Any]


def _m2m_app():
    app = FastAPI()
    requests = []
    accepted = []

    @app.middleware("http")
    async def capture_request(request: Request, call_next):
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "api_key": request.headers.get("x-api-key"),
                "content_type": request.headers.get("content-type"),
                "body": await request.json(),
            }
        )
        return await call_next(request)

    @app.post("/api/v1/remote/callback/update-profile")
    async def update_profile(
        payload: UpdateProfileBody, x_api_key: str = Header(..., alias="X-API-Key")
    ):
        if x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="invalid M2M key")
        accepted.append(payload.model_dump())
        return {"success": True}

    return app, requests, accepted


def _workflow(*, merge: bool) -> WorkflowDef:
    """保留生产回写和出口；上游仅保留最终合并，或注入待校验的载荷。"""
    if merge:
        start = make_node(INPUT, "输入", "start", variables=("profile", "notes"))
        merge_node = replace(
            production.WORKFLOW.node_map[production.FINAL_MERGE_CODE],
            predecessors=(INPUT,),
            bindings=(
                Binding("json_str_1", (INPUT, "profile")),
                Binding("json_str_2", (INPUT, "notes")),
            ),
        )
        initial_nodes = (start, merge_node)
    else:
        start = make_node(
            production.FINAL_MERGE_CODE,
            "注入最终载荷",
            "start",
            variables=("merged_json",),
        )
        initial_nodes = (start,)
    return WorkflowDef(
        workflow_id="m2m_writeback_probe",
        display_name="M2M 回写契约验证",
        entries=(start.node_id,),
        exits=(production.END,),
        outputs=production.WORKFLOW.outputs,
        nodes=(
            *initial_nodes,
            production.WORKFLOW.node_map[production.WRITEBACK_HTTP],
            production.WORKFLOW.node_map[production.END],
        ),
    )


async def _run(tmp_path, *, inputs: dict, enabled: bool, merge: bool):
    app, requests, accepted = _m2m_app()
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        profile_api_base_url="http://offline-m2m",
        profile_api_key=API_KEY,
        writeback_enabled=enabled,
        replay_mode="off",
        http_retry_enabled=False,
    )
    limiter = asyncio.Semaphore(4)
    runtime = NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        llm=LlmClient(settings, transport=httpx.ASGITransport(app=app)),
        http=HttpClient(
            settings, transport=httpx.ASGITransport(app=app), request_limiter=limiter
        ),
        extra={},
    )
    scheduler = Scheduler(build_default_registry(), runtime, max_active_nodes=4)
    try:
        outcome = await scheduler.run(RunRequest(workflow=_workflow(merge=merge), inputs=inputs))
    finally:
        await runtime.llm.aclose()
        await runtime.http.aclose()
    return outcome, requests, accepted


@pytest.mark.parametrize("enabled", [True, False])
async def test_real_final_merge_and_writeback_preserve_m2m_payload(tmp_path, enabled):
    profile = {
        "phone": "13800000000",
        "batch_id": '批次"7"\\目录',
        "analysis_result": {
            "customer_profile": {
                "name": '张"先生"\\路径\n第二行',
                "hobbies": ["自驾", "露营 🏕️"],
                "details": {"confidence": 0.91, "confirmed": True, "unknown": None},
            }
        },
    }
    notes = {"basic_notes_updates": [{"attribute": "生活方式", "value": '周末"亲子"\\出游'}]}
    inputs = {
        "profile": json.dumps(profile, ensure_ascii=False),
        "notes": json.dumps(notes, ensure_ascii=False),
    }
    expected = production.merge_final_data(inputs["profile"], inputs["notes"])["merged_json"]
    expected_body = json.loads(expected)
    outcome, requests, accepted = await _run(
        tmp_path, inputs=inputs, enabled=enabled, merge=True
    )

    assert outcome.succeeded, outcome.error
    assert outcome.outputs == {"result1": expected}
    assert isinstance(outcome.outputs["result1"], str)
    writeback = next(
        node for node in outcome.node_executions if node.node_id == production.WRITEBACK_HTTP
    )
    if enabled:
        assert accepted == [expected_body]
        assert requests == [
            {
                "method": "POST",
                "path": "/api/v1/remote/callback/update-profile",
                "api_key": API_KEY,
                "content_type": "application/json",
                "body": expected_body,
            }
        ]
        assert writeback.outputs["status"] == 200
    else:
        assert requests == accepted == []
        assert writeback.outputs["status"] == 0
        assert json.loads(writeback.outputs["body"])["writeback_skipped"] is True


async def test_writeback_accepts_an_already_parsed_json_object(tmp_path):
    payload = {"phone": "13800000000", "batch_id": "b1", "analysis_result": {"notes": []}}
    outcome, requests, accepted = await _run(
        tmp_path, inputs={"merged_json": payload}, enabled=True, merge=False
    )
    assert outcome.succeeded, outcome.error
    assert len(requests) == 1
    assert requests[0]["body"] == payload
    assert accepted == [payload]


@pytest.mark.parametrize("payload", ['{"phone":', "[]", '"string"', "null"])
async def test_invalid_writeback_payload_fails_before_http(tmp_path, payload):
    outcome, requests, accepted = await _run(
        tmp_path, inputs={"merged_json": payload}, enabled=True, merge=False
    )

    assert outcome.status == RunStatus.FAILED
    assert requests == accepted == []
    writeback = next(
        node for node in outcome.node_executions if node.node_id == production.WRITEBACK_HTTP
    )
    assert writeback.status == NodeStatus.FAILED
    assert writeback.error
    end = next(node for node in outcome.node_executions if node.node_id == production.END)
    assert end.status == NodeStatus.BLOCKED

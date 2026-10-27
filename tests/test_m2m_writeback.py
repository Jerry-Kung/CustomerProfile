"""回写节点对接 M2M JSON 契约；HTTP 仅走本地 ASGI。

**2026-10-27 改指 pipeline**：本文件原用 ``customer_profile_production`` 的合并/回写/
出口三个节点，该臂已整臂退役删除（见 ``docs/history/旧版枚举键与production臂整改方案.md``）。
现改用 ``customer_profile_pipeline`` 的 ``pipeline_callback_writeback`` 与
``pipeline_end``——生产上真正在跑的就是这条链路，断言因此比原先更贴近真实。

节点形状不变：回写节点的请求体由 ``@body_ref`` 整段取自绑定值（不是 ``@body_template``），
因为契约要求 ``analysis_result`` 是**对象**，而模板渲染只会产出字符串。
"""

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
from customer_profile.workflows import customer_profile_pipeline as pipeline

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


def _workflow() -> WorkflowDef:
    """注入待回写的载荷，保留生产的回写与出口两个节点。

    把回写节点与出口节点对 ``pipeline_callback_build`` 的绑定改指本图的 start，
    其余（路径、鉴权、``@writeback`` 闸门、输出形状）一字不动——
    本文件验的是「节点是否照契约把载荷发出去」，不是「载荷怎么拼出来的」。
    """
    start = make_node(INPUT, "注入回写载荷", "start", variables=("merged_json",))
    write = replace(
        pipeline.WORKFLOW.node_map[pipeline.CALLBACK_WRITE],
        predecessors=(INPUT,),
        bindings=(Binding("@body_ref", (INPUT, "merged_json")),),
    )
    end = replace(
        pipeline.WORKFLOW.node_map[pipeline.END],
        predecessors=(pipeline.CALLBACK_WRITE,),
        # ``result1`` 取**待回写的那份载荷**（生产中取自 ``pipeline_callback_build``），
        # 不取回写响应——运行台要展示的是画像结果本身，不是 ``{"success": true}``。
        bindings=(Binding("result1", (INPUT, "merged_json")),),
    )
    return WorkflowDef(
        workflow_id="m2m_writeback_probe",
        display_name="M2M 回写契约验证",
        entries=(INPUT,),
        exits=(end.node_id,),
        outputs={"result1": "result1"},
        nodes=(start, write, end),
    )


async def _run(tmp_path, *, inputs: dict, enabled: bool):
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
        outcome = await scheduler.run(RunRequest(workflow=_workflow(), inputs=inputs))
    finally:
        await runtime.llm.aclose()
        await runtime.http.aclose()
    return outcome, requests, accepted


def _payload() -> dict[str, Any]:
    return {
        "phone": "13800000000",
        "batch_id": '批次"7"\\目录',
        "analysis_result": {
            "profile_summary": {"name": '张"先生"\\路径\n第二行', "gender": "男"},
            "basic_notes_updates": [
                {
                    "attribute_key": "customer_name",
                    "attribute_value": "张先生",
                    "reasoning_detail": {
                        "summary": "客户自我介绍",
                        "steps": [],
                        "evidence": [],
                        "confidence": 0.9,
                    },
                }
            ],
            "ai_insight": {"purchase_intent_level": {"value": "C"}},
        },
    }


@pytest.mark.parametrize("enabled", [True, False])
async def test_writeback_posts_exact_m2m_payload(tmp_path, enabled):
    """开启回写时逐字符原样发出；关闭时不发任何请求，且产出「已跳过」。"""
    payload = _payload()
    outcome, requests, accepted = await _run(
        tmp_path, inputs={"merged_json": payload}, enabled=enabled
    )

    assert outcome.succeeded, outcome.error
    writeback = next(
        node for node in outcome.node_executions if node.node_id == pipeline.CALLBACK_WRITE
    )
    if enabled:
        assert accepted == [payload]
        assert requests == [
            {
                "method": "POST",
                "path": "/api/v1/remote/callback/update-profile",
                "api_key": API_KEY,
                "content_type": "application/json",
                "body": payload,
            }
        ]
        assert writeback.outputs["status"] == 200
    else:
        assert requests == accepted == []
        assert writeback.outputs["status"] == 0
        assert json.loads(writeback.outputs["body"])["writeback_skipped"] is True


async def test_writeback_preserves_nested_objects(tmp_path):
    """``analysis_result`` 必须是**对象**而不是 JSON 字符串——契约的硬要求。

    这一条是回写节点用 ``@body_ref`` 而非 ``@body_template`` 的理由：模板渲染恒产出字符串。
    """
    payload = _payload()
    outcome, requests, accepted = await _run(
        tmp_path, inputs={"merged_json": payload}, enabled=True
    )
    assert outcome.succeeded, outcome.error
    body = accepted[0]
    assert isinstance(body["analysis_result"], dict), "analysis_result 不能被序列化成字符串"
    assert body["analysis_result"]["profile_summary"]["name"] == '张"先生"\\路径\n第二行'


async def test_result1_is_the_writeback_body(tmp_path):
    """运行台展示的 ``result1`` 就是回传体本身。"""
    payload = _payload()
    outcome, *_ = await _run(tmp_path, inputs={"merged_json": payload}, enabled=True)
    assert outcome.succeeded, outcome.error
    assert json.loads(outcome.outputs["result1"]) == payload


@pytest.mark.parametrize("payload", ['{"phone":', "[]", '"string"', "null"])
async def test_invalid_writeback_payload_fails_before_http(tmp_path, payload):
    outcome, requests, accepted = await _run(
        tmp_path, inputs={"merged_json": payload}, enabled=True
    )

    assert outcome.status == RunStatus.FAILED
    assert requests == accepted == []
    writeback = next(
        node for node in outcome.node_executions if node.node_id == pipeline.CALLBACK_WRITE
    )
    assert writeback.status == NodeStatus.FAILED
    assert writeback.error
    end = next(node for node in outcome.node_executions if node.node_id == pipeline.END)
    assert end.status == NodeStatus.BLOCKED

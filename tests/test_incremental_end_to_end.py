"""增量入口端到端：T0 零 LLM、T3 落基线、T3 走全量管线。

用**真实的增量入口定义 + 真实调度器**，只把 T3 的 ``customer_profile_pipeline`` 换成
同 ID 的微缩图（形状一致、内容固定）。要证的是「入口怎么分流、基线怎么读怎么写」，
不是管线内部逻辑——那由 ``tests/test_pipeline_*.py`` 各自覆盖。

T0 的验收是**零 LLM 调用**（``03`` T1-4）：这不是性能指标，而是「不变则不变」的
直接证据——只要有一次模型调用，回传体就不可能逐字等于上一版（实测同输入两次全量
平均只有 66.4% 字段一致）。
"""

from __future__ import annotations

import json

import pytest

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.incremental.compat import compat_key
from customer_profile.incremental.diff import record_key
from customer_profile.persistence.profile_baselines import BaselineRepository, ProfileBaseline
from customer_profile.replay import ReplaySource, http_key
from customer_profile.runner import build_service
from customer_profile.settings import Settings
from customer_profile.workflows import customer_profile_incremental as inc

from .conftest import make_settings

PHONE = "13900000001"


def _history(rows: list[dict]) -> list[dict]:
    return [
        {
            "method": "GET",
            "url": f"/api/v1/remote/data/history/{PHONE}",
            "status_code": 200,
            "text": json.dumps(rows, ensure_ascii=False),
            "headers": {"content-type": "application/json"},
        }
    ]


def _frame(rows: list[dict], profile: dict | None = None) -> dict:
    """固定响应：history 与 profile 两条 GET。

    P2 起入口要**拉一次接口画像**当底座（``01`` §2.2），因此 profile 也必须给固定帧——
    不给的话（严格回放）入口第一个节点就失败。
    """
    headers = {"content-type": "application/json"}
    return {
        http_key("GET", f"/api/v1/remote/data/history/{PHONE}"): [
            {"status_code": 200, "text": json.dumps(rows, ensure_ascii=False), "headers": headers}
        ],
        http_key("GET", f"/api/v1/remote/data/profile/{PHONE}"): [
            {"status_code": 200, "text": json.dumps(profile or {}, ensure_ascii=False), "headers": headers}
        ],
    }


def _row(channel: str, source_key: str, version: int = 1) -> dict:
    return {
        "channel": channel,
        "source_key": source_key,
        "source_version": version,
        "created_at": "2026-11-01T10:00:00",
        "raw_payload": {"content": f"{channel}-{source_key}"},
    }


def _micro_pipeline() -> WorkflowDef:
    """与真管线同 ID 的微缩图：start → 模板 → end，产出 ``result1``。"""
    return WorkflowDef(
        workflow_id=inc.PIPELINE_WORKFLOW,
        display_name="全量重建（微缩）",
        source_dsl=None,
        entries=("m_start",),
        exits=("m_end",),
        outputs={"result1": "result1"},
        nodes=(
            make_node(
                "m_start",
                "输入",
                "start",
                variables=("phone_number", "batch_id", "incremental_ctx"),
                required_incremental_ctx=False,
                defaults={"incremental_ctx": ""},
            ),
            make_node(
                "m_body",
                "回传体",
                "template-transform",
                after=("m_start",),
                inputs={"phone_number": ("m_start", "phone_number")},
                outputs=("output",),
                template_text='{"phone": "{{ phone_number }}", "analysis_result": {"full": true}}',
            ),
            make_node(
                "m_end",
                "输出",
                "end",
                after=("m_body",),
                inputs={"result1": ("m_body", "output")},
                outputs=("result1",),
            ),
        ),
    )


async def _service(
    tmp_path, rows: list[dict], *, baseline: ProfileBaseline | None = None, profile: dict | None = None
):
    settings = make_settings(
        tmp_path,
        incremental_enabled=True,
        baseline_enabled=True,
        # 回写关闭：T0 分支的写回节点会因此只产出「已跳过」，不发出任何请求。
        writeback_enabled=False,
    )
    replay = ReplaySource(http=_frame(rows, profile), llm={}, strict=True, name="incremental")
    service = await build_service(settings, replay=replay)
    # T3 子运行换微缩图：SubWorkflowRunner 持有一份定义副本，必须按 ID 重新登记。
    service.runtime.subworkflow_runner.register(_micro_pipeline())
    if baseline is not None:
        await BaselineRepository(service.store).save(baseline)
    return service


async def test_t0_is_byte_identical_and_calls_no_llm(tmp_path):
    row = _row("OntologyOrder", "k1")
    saved = {
        "phone": PHONE,
        "batch_id": "old-batch",
        "analysis_result": {
            "profile_summary": {"name": "张三"},
            "basic_notes_updates": [
                {"attribute_key": "customer_name", "attribute_value": "张三"}
            ],
        },
    }
    baseline = ProfileBaseline(
        phone=PHONE,
        compat_key=compat_key(),
        updated_at_ms=1,
        stage="lead",
        deal_level="B",
        consumed_keys=(record_key(row),),
        callback=saved,
    )
    service = await _service(tmp_path, [row], baseline=baseline)
    try:
        outcome = await service.run(
            inc.WORKFLOW_ID, {"phone_number": PHONE, "batch_id": "new-batch"}
        )
        assert outcome.succeeded, outcome.error
        # ---- 零 LLM：没有任何一条 llm 尝试
        attempts = await service.store.list_attempts(outcome.run_id)
        assert [a for a in attempts if a.get("kind") == "llm"] == []
        # ---- 回传体除 batch_id 外逐字节相同
        result = json.loads(outcome.outputs["result1"])
        assert result["batch_id"] == "new-batch"
        assert result["analysis_result"] == saved["analysis_result"]
        # ---- 档位留痕
        dispatch = outcome.node(inc.DISPATCH)
        assert dispatch is not None
        assert dispatch.outputs["tier"] == "T0"
        # ---- 基线被换成本次批次，键集合不变
        loaded = await BaselineRepository(service.store).load(PHONE)
        assert loaded is not None
        assert loaded.callback["batch_id"] == "new-batch"
        assert loaded.consumed_keys == (record_key(row),)
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_t3_runs_full_pipeline_and_saves_baseline(tmp_path):
    row = _row("OntologyOrder", "k1")
    service = await _service(tmp_path, [row], baseline=None)
    try:
        outcome = await service.run(
            inc.WORKFLOW_ID, {"phone_number": PHONE, "batch_id": "b-1"}
        )
        assert outcome.succeeded, outcome.error
        dispatch = outcome.node(inc.DISPATCH)
        assert dispatch is not None
        assert dispatch.outputs["tier"] == "T3"
        # 子运行真的跑了（T3 是 tool 节点）
        full = outcome.node(inc.FULL_RUN)
        assert full is not None and full.sub_run_id
        # 基线落库：回传体就是我们发出的那一份
        loaded = await BaselineRepository(service.store).load(PHONE)
        assert loaded is not None
        assert loaded.callback["analysis_result"]["full"] is True
        assert loaded.consumed_keys == (record_key(row),)
        assert loaded.compat_key == compat_key()
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_compat_mismatch_forces_full_rebuild(tmp_path):
    row = _row("OntologyOrder", "k1")
    stale = ProfileBaseline(
        phone=PHONE,
        compat_key="stale-key",
        updated_at_ms=1,
        consumed_keys=(record_key(row),),
        callback={"phone": PHONE, "batch_id": "old", "analysis_result": {"old": True}},
    )
    service = await _service(tmp_path, [row], baseline=stale)
    try:
        outcome = await service.run(
            inc.WORKFLOW_ID, {"phone_number": PHONE, "batch_id": "b-2"}
        )
        assert outcome.succeeded, outcome.error
        dispatch = outcome.node(inc.DISPATCH)
        assert dispatch.outputs["tier"] == "T3"
        assert dispatch.outputs["compat_ok"] == "false"
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()


async def test_baseline_write_failure_does_not_fail_run(tmp_path):
    """基线写不进去最多是下一次落全量，不该让一次已算完的运行失败。"""
    row = _row("OntologyOrder", "k1")
    service = await _service(tmp_path, [row], baseline=None)
    try:
        service.store.close()  # 人为切断存储
        outcome = await service.run(
            inc.WORKFLOW_ID, {"phone_number": PHONE, "batch_id": "b-3"}
        )
        # 存储断开时读基线也失败，dispatch 退回 T3；写基线失败但运行仍成功。
        assert outcome.node(inc.COMMIT).outputs["baseline_saved"] == "false"
        assert outcome.succeeded
    finally:
        await service.aclose()

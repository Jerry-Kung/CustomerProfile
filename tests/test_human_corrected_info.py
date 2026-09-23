"""人工确认信息提取工作流的端到端测试（固定响应）。

验证的是**流程等价**：HTTP 读取、响应体透传、code 节点处理、end 输出契约。真实接口
的成功判定协议属 Q1，此处只按缺省口径记录 HTTP 状态，不判定业务成功。

固定响应取自 ``tests/fixtures/replay/human_corrected_info.json``，不含真实客户数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.definitions import RunStatus
from customer_profile.replay import ReplaySource, http_key
from customer_profile.workflows import human_corrected_info as hci

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"


@pytest.fixture
def replay() -> ReplaySource:
    return ReplaySource.from_file(FIXTURE, strict=True)


async def _run(service_factory, replay, phone_number: str):
    service = await service_factory(replay)
    return await service.run(
        hci.WORKFLOW_ID,
        hci.default_inputs(phone_number),
        business_ref=phone_number,
    )


# ---------------------------------------------------------------- 正常路径


async def test_locked_notes_are_extracted(service, replay):
    outcome = await _run(service, replay, "13800000001")

    assert outcome.status == RunStatus.SUCCEEDED, outcome.error
    payload = json.loads(outcome.outputs["result"])
    assert payload == {
        "locked_notes": [
            {"customer_name": {"value": "张三"}},
            {"customer_address": {"value": "武汉市"}},
        ]
    }


async def test_result1_output_is_a_string(service, replay):
    """``result1`` 是字符串契约，不得变成对象（§3.1）。"""
    outcome = await _run(service, replay, "13800000001")
    assert isinstance(outcome.outputs["result"], str)


async def test_is_locked_accepts_int_string_and_bool(service, replay):
    """DSL 的 ``_is_locked`` 兼容 ``1`` / ``'1'`` / ``True`` 三种写法。"""
    outcome = await _run(service, replay, "13800000003")
    payload = json.loads(outcome.outputs["result"])
    assert payload == {"locked_notes": [{"customer_phone_brand": {"value": "华为"}}]}


async def test_code_fence_in_response_is_tolerated(service, replay):
    """响应体带 ```json 包裹时仍能解析——``_strip_code_fence`` 就是为它写的。"""
    outcome = await _run(service, replay, "13800000003")
    assert outcome.status == RunStatus.SUCCEEDED


async def test_empty_basic_notes_gives_empty_list(service, replay):
    outcome = await _run(service, replay, "13800000002")
    assert json.loads(outcome.outputs["result"]) == {"locked_notes": []}


async def test_unlocked_fields_are_excluded(service, replay):
    outcome = await _run(service, replay, "13800000001")
    payload = json.loads(outcome.outputs["result"])
    names = [list(entry)[0] for entry in payload["locked_notes"]]
    assert "customer_nick" not in names
    assert "customer_remark" not in names


# ---------------------------------------------------------------- 非 2xx


async def test_http_404_retries_for_get_then_fails(service, replay):
    """GET 幂等，按配置重试；重试用尽后运行失败。

    只提供 1 条固定响应，第 2 次尝试会因 fixture 用尽而按最后一条重复返回，
    因此这里断言「最终失败」而不是「尝试了 N 次」——次数由配置决定，测试不复制
    实现里的算式。
    """
    outcome = await _run(service, replay, "13800000004")
    assert outcome.status == RunStatus.FAILED
    assert "404" in (outcome.error or "")


async def test_failed_http_keeps_node_error_and_attempts(service, replay):
    service_obj = await service(replay)
    outcome = await service_obj.run(hci.WORKFLOW_ID, hci.default_inputs("13800000004"))

    http_node = outcome.node(hci.HTTP_NODE)
    assert http_node is not None and http_node.status == "failed"

    attempts = await service_obj.store.list_attempts(outcome.run_id, node_id=hci.HTTP_NODE)
    assert len(attempts) >= 2, "GET 失败后应留下多次尝试记录"
    assert all(a["status_code"] == 404 for a in attempts)
    assert all(a["replayed"] == 1 for a in attempts)


# ---------------------------------------------------------------- 留痕


async def test_every_node_has_a_record(service, replay):
    service_obj = await service(replay)
    outcome = await service_obj.run(hci.WORKFLOW_ID, hci.default_inputs("13800000001"))

    rows = await service_obj.store.list_node_executions(outcome.run_id)
    recorded = {row["node_id"] for row in rows}
    expected = {node.node_id for node in hci.WORKFLOW.nodes}
    assert recorded == expected


async def test_http_attempt_records_url_and_method(service, replay):
    service_obj = await service(replay)
    outcome = await service_obj.run(hci.WORKFLOW_ID, hci.default_inputs("13800000001"))

    attempts = await service_obj.store.list_attempts(outcome.run_id, node_id=hci.HTTP_NODE)
    assert attempts, "HTTP 节点没有留下尝试记录"
    first = attempts[0]
    assert first["kind"] == "http"
    assert first["target"] == http_key("GET", f"/api/v1/remote/data/profile/13800000001")


async def test_credentials_never_reach_the_ledger(service, replay):
    """密钥永不落痕。回放模式没有真密钥，但掩码逻辑必须始终生效。"""
    service_obj = await service(replay)
    outcome = await service_obj.run(hci.WORKFLOW_ID, hci.default_inputs("13800000001"))

    attempts = await service_obj.store.list_attempts(outcome.run_id)
    serialised = json.dumps(attempts, ensure_ascii=False)
    assert "test-key" not in serialised
    assert "sk-" not in serialised


async def test_run_binds_definition_version(service, replay):
    """运行绑定当次定义版本，新版本不覆盖旧运行的展示。"""
    service_obj = await service(replay)
    outcome = await service_obj.run(hci.WORKFLOW_ID, hci.default_inputs("13800000001"))

    row = await service_obj.store.get_run(outcome.run_id)
    assert row["definition_version_id"] is not None

    version = await service_obj.store.definition_version(row["definition_version_id"])
    assert version["workflow_id"] == hci.WORKFLOW_ID
    assert version["definition"]["workflow_id"] == hci.WORKFLOW_ID
    node_ids = {n["node_id"] for n in version["definition"]["nodes"]}
    assert node_ids == {node.node_id for node in hci.WORKFLOW.nodes}


async def test_business_ref_is_recorded(service, replay):
    service_obj = await service(replay)
    # business_ref 是提交时传入的业务标识，必须显式给出才会被记录
    outcome = await service_obj.run(
        hci.WORKFLOW_ID,
        hci.default_inputs("13800000001"),
        business_ref="13800000001",
    )

    row = await service_obj.store.get_run(outcome.run_id)
    assert row["business_ref"] == "13800000001"


# ---------------------------------------------------------------- 定义细节


def test_path_and_phone_are_concatenated_not_replaced():
    """路径拼接不做通用字符串替换：字面路径 + 绑定值。"""
    assert hci.profile_url("13800000001") == "/api/v1/remote/data/profile/13800000001"


def test_no_secret_in_definition_or_fixture():
    """定义与固定响应中都不含密钥形状的字符串。"""
    serialised = json.dumps(hci.WORKFLOW.asdict(), ensure_ascii=False)
    assert "X-API-Key" not in serialised
    assert "sk-" not in serialised

    fixture_text = FIXTURE.read_text(encoding="utf-8")
    assert "sk-" not in fixture_text

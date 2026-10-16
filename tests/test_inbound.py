"""对外任务接口（V0.5.5）。

四条口径各有「看起来正常」的失效方式，专门钉住：

1. **空密钥被当成「不校验」。** 对外写面一旦放行无密钥请求，端口就是任何人可写的入口：
   一条外部请求会变成一次真实算力消耗与一次生产回写。因此这里断言「未配置 → 503」，
   且**不得**返回 200。
2. **接单进程把自己当 worker。** ``inline_worker`` 若为真，21 分钟的工作流会跑在接收
   进程里；``interrupt_on_start`` 若为真，一次接单进程重启会把**别的进程**正在跑的
   运行标成 interrupted。两者都用 ``inbound_settings`` 强制关掉并在此断言。
3. **手机号校验只写在执行期。** 与 ``test_submit_gate`` 同口径：非法入参若是靠 ``start``
   节点报错，调用方拿到的是一条「已接收」的 200，错误与结果对不上。
4. **``batch_id`` 丢了。** 它是回传体顶层三键之一（前端据此判断任务来源），而工作流定义
   里它是**可选**的。接口层必填是一道门，但真正的证据是「落库的入参里有它、且一路到
   回写体」——中间任何一处把它吃掉，单据来源都会静默变成空。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from customer_profile import inbound as ib
from customer_profile.api import PHONE_PATTERN
from customer_profile.persistence import Store
from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.settings import Settings
from customer_profile.workflows import customer_profile_pipeline as pipeline

from .conftest import make_settings

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"
)

KEY = "test-inbound-key-0123456789abcdef"
PHONE = "13415100087"
BATCH = "batch-inbound-001"


def _settings(tmp_path, **overrides) -> Settings:
    base = {"inbound_api_key": KEY, "inline_worker": False}
    base.update(overrides)
    return make_settings(tmp_path, **base)


async def _client(tmp_path, settings: Settings | None = None):
    """装配入站应用 + ASGI 客户端。

    服务用固定响应装配（不出网、不写生产库），与 ``test_api.py`` 同一手法。
    """
    async def factory():
        return await build_service(
            settings or _settings(tmp_path),
            replay=ReplaySource.from_file(FIXTURE, strict=True),
        )

    app = ib.create_inbound_app(service_factory=factory)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


def _headers(key: str | None = KEY) -> dict[str, str]:
    return {"X-API-Key": key} if key is not None else {}


def _inputs_in_db(tmp_path) -> dict:
    """直接查库取那条运行的入参。

    不走 ``service.store``：lifespan 退出时 store 已关闭；直接查库同时也绕开
    「用被测对象验证自己」的问题。
    """
    import json

    db = tmp_path / "test.db"
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT inputs_json FROM workflow_runs WHERE parent_run_id IS NULL"
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        connection.close()


# ---------------------------------------------------------------- 鉴权


async def test_missing_key_is_401(tmp_path):
    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": BATCH},
                headers=_headers(None),
            )
    assert response.status_code == 401, response.text


async def test_wrong_key_is_403(tmp_path):
    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": BATCH},
                headers=_headers("not-the-key"),
            )
    assert response.status_code == 403, response.text


async def test_unconfigured_key_closes_the_interface(tmp_path):
    """未配置 ``INBOUND_API_KEY`` 时接口**关闭**（503），而不是放行。

    这是本文件最重要的一条：空密钥若被当作「不校验」，这个端口就是任何人可写的入口。
    """
    app, client = await _client(tmp_path, _settings(tmp_path, inbound_api_key=""))
    async with client as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": BATCH},
                headers=_headers(KEY),
            )
    assert response.status_code == 503, response.text
    # 被拒的请求不得留下运行记录
    assert _inputs_in_db(tmp_path) == {}


async def test_health_does_not_leak_whether_the_interface_is_enabled(tmp_path):
    """存活探针不报告「接口是否已启用」——那是未鉴权调用方不该知道的信息。"""
    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            body = (await http.get("/health")).json()
    assert body["status"] == "ok"
    assert "启用" not in str(body) and "enabled" not in str(body)


# ---------------------------------------------------------------- 接单


async def test_valid_request_is_accepted_and_persisted(tmp_path):
    """200 + run_id，且任务在返回前已落库（``status='queued'``）。"""
    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": BATCH},
                headers=_headers(),
            )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    assert body["workflow_id"] == ib.ENTRY_WORKFLOW_ID

    db = tmp_path / "test.db"
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT status, business_ref FROM workflow_runs WHERE run_id = ?",
            (body["run_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "接单返回了 run_id，但库里查不到——「先落库再返回」不成立"
    assert row[0] == "queued"


async def test_batch_id_reaches_the_database(tmp_path):
    """``batch_id`` 必须进落库的入参——它是回传体来源标记的唯一来源。"""
    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": BATCH},
                headers=_headers(),
            )
    inputs = _inputs_in_db(tmp_path)
    assert inputs.get("batch_id") == BATCH
    assert inputs.get("phone_number") == PHONE


async def test_batch_id_is_required(tmp_path):
    """工作流定义里 ``batch_id`` 可选，接口层必须把它变成必填。"""
    app, client = await _client(tmp_path)
    async with app.router.lifespan_context(app):
        async with client as http:
            blank = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE, "batch_id": "   "},
                headers=_headers(),
            )
            missing = await http.post(
                ib.TASK_PATH,
                json={"phone_number": PHONE},
                headers=_headers(),
            )
    assert blank.status_code == 422, blank.text
    assert missing.status_code == 422, missing.text


async def test_invalid_phone_is_rejected_before_the_run(tmp_path):
    app, client = await _client(tmp_path)
    async with app.router.lifespan_context(app):
        async with client as http:
            response = await http.post(
                ib.TASK_PATH,
                json={"phone_number": "12345", "batch_id": BATCH},
                headers=_headers(),
            )
    assert response.status_code == 422, response.text
    assert "phone_number" in response.text
    assert _inputs_in_db(tmp_path) == {}


def test_phone_shape_matches_the_existing_entry_contract():
    """接口与运行台提交页、脚本共用同一形状的校验。"""
    assert PHONE_PATTERN.match(PHONE)
    assert not PHONE_PATTERN.match("12345")
    assert not PHONE_PATTERN.match("213415100087")


# ---------------------------------------------------------------- 进程定位


def test_inbound_process_does_not_consume_the_queue_or_interrupt_runs(tmp_path):
    """接单进程既不执行工作流，也不动别人的运行状态。

    两条都由 ``inbound_settings`` 强制，且**不通过 ``.env`` 配置**——它们是这个进程的
    定位，不是可调参数。``interrupt_on_start`` 尤其要紧：入站进程与运行台、worker
    共用同一个 SQLite，由它标 interrupted 会改坏别的进程正在跑的运行。
    """
    base = make_settings(
        tmp_path, inbound_api_key=KEY, inline_worker=True, interrupt_on_start=True
    )
    resolved = ib.inbound_settings(base)
    assert resolved.inline_worker is False
    assert resolved.interrupt_on_start is False
    # 其余配置原样透传
    assert resolved.inbound_api_key == KEY
    assert resolved.database_url == base.database_url


async def test_inbound_service_does_not_start_an_inline_worker(tmp_path):
    app, client = await _client(tmp_path)
    async with app.router.lifespan_context(app):
        service = app.state.service
        assert service.inline_worker is None
        assert service.inline_worker_task is None
        assert service.settings.interrupt_on_start is False


# ---------------------------------------------------------------- 端到端接线


async def test_batch_id_binding_reaches_the_writeback_body():
    """定义层证据：``batch_id`` 从 ``start`` 一路绑到回写体构造与回写节点。

    上面几条证明「入参进库」；这一条证明「库里的值会被用上」。两段**都必须**有：
    只测入参，接线断了也全绿；只测接线，接口层漏传也全绿。
    """
    start = pipeline.WORKFLOW.node_map[pipeline.START]
    callback_build = pipeline.WORKFLOW.node_map[pipeline.CALLBACK_BUILD]
    writeback = pipeline.WORKFLOW.node_map[pipeline.CALLBACK_WRITE]

    assert "batch_id" in start.config["variables"]
    assert (pipeline.START, "batch_id") in {
        (b.source[0], b.source[1]) for b in callback_build.bindings
    }, "回传体构造节点没有绑定 start 的 batch_id"
    assert (pipeline.CALLBACK_BUILD, "callback_json") in {
        (b.source[0], b.source[1]) for b in writeback.bindings
    }, "回写节点没有取回传体构造节点的产出"
    assert writeback.config.get("@body_ref") is True
    assert writeback.config.get("@writeback") is True
    assert writeback.config.get("@path", "").endswith("callback/update-profile")


def test_callback_build_puts_batch_id_in_the_body():
    """回传体顶层三键含 ``batch_id``，且取值就是传进来的那一个。"""
    from customer_profile import callback_build

    payload = callback_build.build_analysis_result(
        phone=PHONE, batch_id=BATCH, facts_json="{}", notes_updates_json="[]",
        express_json="{}",
    )
    import json

    body = json.loads(payload)
    assert set(body) == {"phone", "batch_id", "analysis_result"}
    assert body["batch_id"] == BATCH
    assert body["phone"] == PHONE


async def test_accepted_run_is_executable_by_a_worker(tmp_path):
    """接单落库的运行必须能被 worker 领走并跑完。

    为什么用 ``human_corrected_info`` 而不是业务测试臂：后者的真实链路需要覆盖
    14 路扇出与全部提示词的录制 fixture，体积与维护成本都不合理（``test_entry_end_to_end.py``
    为此把子流程换成同形状微缩图）。本用例要证的是**入站接单与队列消费之间的接线**——
    领取、入参解码、执行、终态回写——这与跑哪条工作流无关。

    业务测试臂那一段由上面两条定义层用例钉住：入参进库、绑定到回写体。
    """
    from customer_profile.workflows import human_corrected_info as hci

    app, client = await _client(tmp_path)
    async with client as http:
        async with app.router.lifespan_context(app):
            service = app.state.service
            # 入站端点写死业务测试臂，故这里不经 HTTP，只验证「入队的那条能被消费」。
            run_id = await service.submit(
                hci.WORKFLOW_ID, {"phone_number": PHONE}, business_ref=PHONE
            )
            assert run_id

            # 同一个 store 上模拟 worker 的一轮领取
            from customer_profile.worker import Worker

            worker = Worker(service, service.settings)
            assert await worker.run_once(), "队列里有待跑运行，worker 却领不到"

            # 断言必须在 lifespan 内做：退出时 store 会被关闭（``api.py`` 同口径）。
            detail = await service.store.get_run(run_id)
            assert detail is not None
            assert detail["status"] in {"succeeded", "failed"}, detail["status"]

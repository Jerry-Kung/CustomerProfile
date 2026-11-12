"""MySQL 后端的方言契约测试。

**为什么单独一个文件。** 默认套件必须无外部依赖、在干净检出上全绿；把 MySQL 用例
混进去之后，任何一次未配库的执行都会变成一堆 skip，久而久之没人再看它们。

**怎么跑。** 需要一条可写的 ``CUSTOMER_PROFILE_MYSQL_URL``（或 ``DATABASE_URL``
以 ``mysql://`` 开头），并且库里允许建表：

    set CUSTOMER_PROFILE_MYSQL_URL=mysql://user:pass@host:3306/customer_profile_runtime
    python -m pytest tests/test_mysql_backend.py -v

未配置时整体 skip，不是失败——这条测试的价值在于「有人主动跑过」，而不是常驻红灯。

**每个用例都在自己的表前缀下跑**是做不到的（表名写死在 SQL 里），因此每个用例开头
清空四张表。这要求测试库是**专用的**，绝不能指向业务库；用例会先检查这一点。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from customer_profile.definitions import RunStatus
from customer_profile.persistence.dbapi import build_backend, convert_placeholders
from customer_profile.persistence.schema import RunRecordRow
from customer_profile.persistence.store import Store

pytestmark = pytest.mark.mysql_integration


def _url() -> str | None:
    raw = os.environ.get("CUSTOMER_PROFILE_MYSQL_URL", "").strip()
    if raw.startswith("mysql://"):
        return raw
    fallback = os.environ.get("DATABASE_URL", "").strip()
    return fallback if fallback.startswith("mysql://") else None


MYSQL_URL = _url()

pytestmark = [
    pytest.mark.mysql_integration,
    pytest.mark.skipif(MYSQL_URL is None, reason="未配置 CUSTOMER_PROFILE_MYSQL_URL"),
]

RUNTIME_TABLES = ("request_attempts", "node_executions", "workflow_runs",
                  "profile_baselines", "definition_versions")


@pytest.fixture
async def store():
    backend = build_backend(MYSQL_URL)  # type: ignore[arg-type]
    # 安全闸：拒绝在「有业务表」的 schema 上清表。
    business = {"customer_profile_main", "customer_invite_main", "customer_profile_tasks"}
    instance = Store(backend, migrate=True)
    instance.connect()
    present = await instance._read(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE()"
    )
    names = {row["name"] for row in present}
    if names & business:
        instance.close()
        pytest.fail(
            "CUSTOMER_PROFILE_MYSQL_URL 指向了业务库（含 "
            f"{sorted(names & business)}）；请改用专用的运行时 schema"
        )
    # **前后各复原一次结构**：有几句用例会故意 DROP COLUMN / DROP TABLE 来验证自愈能力，
    # 不复原就会把「库结构损坏」留给下一次运行——而服务启动时的结构校验会因此拒绝连接，
    # 看起来像是代码坏了。
    await _heal(instance)
    await _truncate(instance)
    try:
        yield instance
    finally:
        await _truncate(instance)
        await _heal(instance)
        instance.close()


async def _truncate(store: Store) -> None:
    def work(connection: Any) -> None:
        for table in RUNTIME_TABLES:
            connection.execute(f"DELETE FROM {table}")
        connection.commit()

    await store._run(work)


def _run_row(run_id: str, *, status: str = RunStatus.QUEUED, created: int = 1) -> RunRecordRow:
    return RunRecordRow(
        run_id=run_id,
        workflow_id="customer_profile_pipeline",
        status=status,
        inputs={"phone_number": "13800000000"},
        queued_at_ms=created,
        created_at_ms=created,
    )


# ---------------------------------------------------------------- 占位符


def test_placeholders_skip_string_literals():
    assert convert_placeholders("a = ? AND b = ?", "%s") == "a = %s AND b = %s"
    # 引号内的 ? 不该被改写：LIKE 之类的字面量里可能出现
    assert convert_placeholders("x LIKE 'a?b' AND y = ?", "%s") == "x LIKE 'a?b' AND y = %s"
    assert convert_placeholders("x = 'it''s ?' AND y = ?", "%s") == "x = 'it''s ?' AND y = %s"


# ---------------------------------------------------------------- 建表与幂等


async def test_schema_is_idempotent(store):
    """再次 ensure_schema 不应报错，也不该丢数据。"""
    from customer_profile.persistence.mysql_schema import ensure_mysql_schema

    await store.insert_run(_run_row("a" * 32))
    await store._run(lambda c: (ensure_mysql_schema(c), None)[1])
    assert await store.count_runs() == 1


async def test_insert_run_is_idempotent_upsert(store):
    rid = "b" * 32
    await store.insert_run(_run_row(rid, created=5))
    await store.insert_run(
        RunRecordRow(
            run_id=rid,
            workflow_id="customer_profile_pipeline",
            status=RunStatus.RUNNING,
            started_at_ms=7,
            created_at_ms=999,
        )
    )
    row = await store.get_run(rid)
    assert row is not None
    assert row["status"] == RunStatus.RUNNING
    assert row["created_at_ms"] == 5, "排序键不得被后来的写入覆盖"
    assert row["started_at_ms"] == 7
    assert await store.count_runs() == 1


async def test_enqueue_run_once_reports_duplicate(store):
    rid = "c" * 32
    assert await store.enqueue_run_once(_run_row(rid)) is True
    assert await store.enqueue_run_once(_run_row(rid)) is False
    assert await store.count_runs() == 1


async def test_enqueue_run_once_never_downgrades_status(store):
    rid = "d" * 32
    await store.enqueue_run_once(_run_row(rid))
    await store.set_run_status(rid, RunStatus.RUNNING)
    assert await store.enqueue_run_once(_run_row(rid)) is False
    row = await store.get_run(rid)
    assert row is not None and row["status"] == RunStatus.RUNNING


# ---------------------------------------------------------------- 队列


async def test_claim_is_atomic_across_connections(store):
    """两个独立连接并发领取，绝不能拿到同一行。"""
    await store.insert_run(_run_row("e" * 32, created=1))
    await store.insert_run(_run_row("f" * 32, created=2))

    other = Store(build_backend(MYSQL_URL), migrate=False)  # type: ignore[arg-type]
    other.connect()
    try:
        first, second = await asyncio.gather(
            store.claim_next_queued_run("w1", lease_ms=60_000),
            other.claim_next_queued_run("w2", lease_ms=60_000),
        )
    finally:
        other.close()

    assert first is not None and second is not None
    assert first["run_id"] != second["run_id"]


async def test_claim_skips_child_runs(store):
    await store.insert_run(
        RunRecordRow(run_id="g" * 32, workflow_id="customer_profile_pipeline",
                     parent_run_id="h" * 32, status=RunStatus.QUEUED,
                     queued_at_ms=1, created_at_ms=1)
    )
    assert await store.claim_next_queued_run("w1", lease_ms=1000) is None


async def test_claim_honours_workflow_scope(store):
    await store.insert_run(_run_row("i" * 32, created=1))
    await store.insert_run(
        RunRecordRow(run_id="j" * 32, workflow_id="invitation_strategy",
                     status=RunStatus.QUEUED, queued_at_ms=1, created_at_ms=2)
    )
    profile = await store.claim_next_queued_run("w1", lease_ms=1000, workflow_scope="profile")
    invitation = await store.claim_next_queued_run("w1", lease_ms=1000, workflow_scope="invitation")
    assert profile is not None and profile["run_id"] == "i" * 32
    assert invitation is not None and invitation["run_id"] == "j" * 32


async def test_renew_lease_reports_found_rows(store):
    """值未变的 UPDATE 也必须报告「匹配到了」——这是 FOUND_ROWS 的意义。"""
    rid = "k" * 32
    await store.insert_run(_run_row(rid))
    claimed = await store.claim_next_queued_run("w1", lease_ms=60_000)
    assert claimed is not None
    assert await store.renew_lease(rid, "w1", lease_ms=60_000) is True
    assert await store.renew_lease(rid, "w1", lease_ms=60_000) is True
    assert await store.renew_lease(rid, "someone-else", lease_ms=60_000) is False


async def test_requeue_expired_leases(store):
    rid = "l" * 32
    await store.insert_run(_run_row(rid))
    await store.claim_next_queued_run("w1", lease_ms=1)
    await asyncio.sleep(0.01)
    assert await store.requeue_expired_leases(limit=10) == 1
    assert (await store.queue_snapshot())["queued"] == 1


# ---------------------------------------------------------------- 恢复


async def test_mark_running_as_interrupted(store):
    """既有语义：根运行与**在跑的**子运行都会被标（递归 CTE 按祖先归属）。

    这不是「只标根」——子运行同样停在 running，不标就会永远挂着。断言这个行为，
    是为了让「SQLite 与 MySQL 的递归查询结果一致」变成会失败的测试。
    """
    root = "m" * 32
    child = "n" * 32
    await store.insert_run(_run_row(root, status=RunStatus.RUNNING))
    await store.insert_run(
        RunRecordRow(run_id=child, workflow_id="customer_profile_pipeline",
                     parent_run_id=root, status=RunStatus.RUNNING, created_at_ms=2)
    )
    marked = await store.mark_running_as_interrupted(workflow_scope="all")
    assert marked == 2
    for rid in (root, child):
        row = await store.get_run(rid)
        assert row is not None and row["status"] == RunStatus.INTERRUPTED


async def test_mark_interrupted_ignores_queued(store):
    """``queued`` 不得被标中断——否则「重启不丢任务」不成立。"""
    rid = "z" * 32
    await store.insert_run(_run_row(rid, status=RunStatus.QUEUED))
    assert await store.mark_running_as_interrupted(workflow_scope="all") == 0
    row = await store.get_run(rid)
    assert row is not None and row["status"] == RunStatus.QUEUED


async def test_abandon_run_only_accepts_interrupted(store):
    rid = "o" * 32
    await store.insert_run(_run_row(rid, status=RunStatus.INTERRUPTED))
    assert await store.abandon_run(rid) is True
    assert await store.abandon_run(rid) is False
    row = await store.get_run(rid)
    assert row is not None and row["status"] == RunStatus.CANCELLED
    assert "人工确认" in (row["error"] or ""), "错误拼接必须走 CONCAT（MySQL 的 || 是逻辑或）"


# ---------------------------------------------------------------- 节点 / 尝试


async def test_node_upsert_keeps_unique_key(store):
    rid = "p" * 32
    await store.insert_run(_run_row(rid))
    for status in ("running", "succeeded"):
        await store.upsert_node_execution(
            run_id=rid, node_id="pipeline_l1_input", node_title="L1",
            node_type="code", call_path="", status=status,
            outputs={"a": 1} if status == "succeeded" else None,
        )
    nodes = await store.list_node_executions(rid)
    assert len(nodes) == 1
    assert nodes[0]["status"] == "succeeded"


async def test_node_upsert_distinguishes_call_paths(store):
    rid = "q" * 32
    await store.insert_run(_run_row(rid))
    for path in ("", "/iter:1/1"):
        await store.upsert_node_execution(
            run_id=rid, node_id="loop_body", node_title="body",
            node_type="code", call_path=path, status="succeeded",
        )
    assert len(await store.list_node_executions(rid)) == 2


async def test_large_payload_round_trip(store):
    """长文本必须原样往返：严格模式下列宽不足会直接报错，而不是截断。"""
    rid = "r" * 32
    blob = "客" * 200_000
    await store.insert_run(
        RunRecordRow(run_id=rid, workflow_id="customer_profile_pipeline",
                     status=RunStatus.SUCCEEDED, outputs={"text": blob},
                     created_at_ms=1)
    )
    row = await store.get_run(rid)
    assert row is not None
    assert row["outputs_json"]["text"] == blob


async def test_attempts_are_appended(store):
    rid = "s" * 32
    await store.insert_run(_run_row(rid))
    for no in (1, 2):
        await store.insert_attempt(
            run_id=rid, node_id="node", call_path="", attempt_no=no,
            kind="llm", target="m", request={"n": no}, response_text="ok",
        )
    attempts = await store.list_attempts(rid)
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    assert await store.count_attempts(rid) == 2


async def test_payload_state_machine(store):
    """in_db → packed → purged 三态必须可区分，不能退化成「空 = 没有」。"""
    from customer_profile.persistence.store import PAYLOAD_PACKED, PAYLOAD_PURGED

    rid = "t" * 32
    await store.insert_run(_run_row(rid, status=RunStatus.SUCCEEDED))
    await store.upsert_node_execution(
        run_id=rid, node_id="n", node_title="n", node_type="code",
        call_path="", status="succeeded", outputs={"big": "x" * 100},
    )
    await store._write(
        "UPDATE workflow_runs SET outputs_json = NULL, payload_state = ? WHERE run_id = ?",
        (PAYLOAD_PACKED, rid),
    )
    _, _, missing = await store.payload_overlay(rid)
    assert missing is True, "packed 但读不到包时应标缺失，而不是返回空载荷"

    await store._write(
        "UPDATE workflow_runs SET payload_state = ? WHERE run_id = ?",
        (PAYLOAD_PURGED, rid),
    )
    state, overlay, missing = await store.payload_overlay(rid)
    assert state == PAYLOAD_PURGED and overlay is None and missing is False


# ---------------------------------------------------------------- 结构自检与自愈


async def _inspect(store: Store):
    from customer_profile.persistence import mysql_schema

    return await store._run(lambda c: (mysql_schema.inspect(c), None)[0])


async def _heal(store: Store):
    from customer_profile.persistence import mysql_schema

    return await store._run(lambda c: (mysql_schema.ensure_schema(c), None)[0])


async def test_inspect_reports_satisfied_on_clean_schema(store):
    report = await _inspect(store)
    assert report.satisfied, report.describe()
    assert report.missing_tables == []
    assert report.missing_columns == {}


async def test_ensure_schema_restores_dropped_column(store):
    """升级带来的新列：自动补上，已有数据不受影响。

    这正是「运维不用记住升级后该补哪一列」的依据。
    """
    rid = "u" * 32
    await store.insert_run(_run_row(rid, status=RunStatus.SUCCEEDED))
    await store._write("ALTER TABLE workflow_runs DROP COLUMN payload_state")

    after_drop = await _inspect(store)
    assert after_drop.missing_columns.get("workflow_runs") == ["payload_state"]
    assert not after_drop.satisfied

    healed = await _heal(store)
    assert healed.satisfied
    assert "workflow_runs.payload_state" in healed.columns_added
    assert await store.count_runs() == 1, "补列不能动已有行"


async def test_ensure_schema_restores_dropped_table(store):
    """整表被误删：只重建缺的那张，其它表不动。"""
    await store._write("DROP TABLE request_attempts")

    after_drop = await _inspect(store)
    assert after_drop.missing_tables == ["request_attempts"]

    healed = await _heal(store)
    assert healed.satisfied
    assert healed.tables_created == ["request_attempts"]


async def test_ensure_schema_is_noop_when_satisfied(store):
    """已就绪的库上重复执行，不报告任何改动。"""
    healed = await _heal(store)
    assert healed.satisfied
    assert healed.tables_created == []
    assert healed.columns_added == []


async def test_assert_expected_version_detects_missing_column(store):
    """版本行还在、但列被删了——必须拒绝启动。

    这是「只看版本表」的漏洞：版本行只证明有人跑过初始化，不证明表还在。
    """
    from customer_profile.persistence import mysql_schema

    await store._write("ALTER TABLE workflow_runs DROP COLUMN payload_state")
    with pytest.raises(RuntimeError, match="workflow_runs.payload_state"):
        await store._run(
            lambda c: (mysql_schema.assert_expected_version(c), None)[1]
        )


"""基线驱动脚本：批次 ID、续跑判定、配置硬编码与单号码落盘。

分两层：

- **纯函数**（批次 ID、产物判定、待跑筛选、配置白名单）直接测，无依赖。
- **``run_one``** 用一个桩替换 ``scheduler.run``，但**往真实临时库里造一行已完成的运行**，
  这样导出链（读库 → 递归 → 落盘）跑的是真代码。桩只负责「产生一次运行的结果」，
  不假装导出成功。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from customer_profile.definitions import RunStatus
from customer_profile.persistence.schema import RunRecordRow
from customer_profile.persistence.store import Store

from .conftest import REPO_ROOT, make_settings

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import baseline_artifacts as ba  # noqa: E402
import run_baseline as rb  # noqa: E402

PHONE = "13415100087"


# ---------------------------------------------------------------- 批次 ID


def test_batch_id_shape():
    """``<UTC 时间戳>-<短提交>``，时间戳可解析回来。"""
    now = datetime(2026, 9, 24, 15, 30, 5, tzinfo=timezone.utc)
    batch_id = rb.make_batch_id(now, "7c4c8cf5aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert batch_id == "20260924T1530Z-7c4c8cf5"


def test_batch_id_without_git_says_so():
    """取不到提交号时写 ``nogit``，不伪造一个 sha。"""
    now = datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc)
    assert rb.make_batch_id(now, None).endswith("-nogit")


def test_batch_id_converts_to_utc():
    """本地时区的时间戳也按 UTC 落 ID——批次跨时区比对时不会错位。"""
    local = datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc)
    assert rb.make_batch_id(local, "abcdef12").startswith("20260924T2330Z")


# ---------------------------------------------------------------- 产物与续跑


def _write_result(batch_dir, phone: str, status: str) -> None:
    target = rb.phone_artifact_dir(batch_dir, phone)
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text(
        json.dumps({"phone": phone, "status": status}), encoding="utf-8"
    )


def test_has_succeeded_artifact_only_counts_succeeded(tmp_path):
    """只有产物存在**且**状态 succeeded 才算完成。"""
    assert rb.has_succeeded_artifact(tmp_path, PHONE) is False

    _write_result(tmp_path, PHONE, RunStatus.FAILED)
    assert rb.has_succeeded_artifact(tmp_path, PHONE) is False

    _write_result(tmp_path, PHONE, RunStatus.SUCCEEDED)
    assert rb.has_succeeded_artifact(tmp_path, PHONE) is True


def test_has_succeeded_artifact_tolerates_corrupt_file(tmp_path):
    """半截/损坏的产物按「未完成」处理，重跑而不是当成已完成。"""
    target = rb.phone_artifact_dir(tmp_path, PHONE)
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text("{not json", encoding="utf-8")
    assert rb.has_succeeded_artifact(tmp_path, PHONE) is False


def test_pending_skips_finished_when_resuming(tmp_path):
    """续跑时跳过已完成号码，只留待跑的。"""
    phones = ["13415100087", "13430349943", "13527911093"]
    _write_result(tmp_path, phones[0], RunStatus.SUCCEEDED)
    _write_result(tmp_path, phones[1], RunStatus.FAILED)

    assert rb.pending_phones(phones, tmp_path, resume=True) == [phones[1], phones[2]]
    assert rb.pending_phones(phones, tmp_path, resume=False) == phones


def test_pending_honours_only_and_rejects_unknown(tmp_path):
    """``--only`` 只跑指定号码；清单里没有的号码直接报错，不静默忽略。"""
    phones = ["13415100087", "13430349943"]
    assert rb.pending_phones(phones, tmp_path, resume=False, only=[phones[1]]) == [
        phones[1]
    ]
    with pytest.raises(SystemExit):
        rb.pending_phones(phones, tmp_path, resume=False, only=["19999999999"])


def test_pending_only_combined_with_resume(tmp_path):
    """``--only`` 与续跑叠加：指定号码里已完成的仍然跳过。"""
    phones = ["13415100087", "13430349943"]
    _write_result(tmp_path, phones[0], RunStatus.SUCCEEDED)
    assert rb.pending_phones(phones, tmp_path, resume=True, only=phones) == [phones[1]]


# ---------------------------------------------------------------- 配置


def test_config_snapshot_excludes_credentials(tmp_path):
    """配置快照只取白名单，凭据绝不进归档目录。"""
    settings = make_settings(
        tmp_path, llm_api_key="sk-secret", profile_api_key="profile-secret"
    )
    snapshot = rb.config_snapshot(settings, {"max_active_nodes": 16})
    blob = json.dumps(snapshot, ensure_ascii=False)
    assert "sk-secret" not in blob
    assert "profile-secret" not in blob
    assert snapshot["max_active_nodes"] == 16
    assert "writeback_enabled" in snapshot


def test_build_settings_forces_baseline_invariants(tmp_path):
    """三处硬编码成立：不回写、不回放、不动别人的 running。"""
    settings = rb.build_settings(tmp_path / "trace.db", read_timeout=1234.0)
    assert settings.writeback_enabled is False
    assert settings.replay_mode == "off"
    assert settings.interrupt_on_start is False
    assert settings.http_read_timeout == 1234.0
    assert settings.max_concurrent_requests == rb.GATE_FOR_CONCURRENCY


def test_build_settings_uses_absolute_db_path(tmp_path):
    """批次库用绝对路径：相对路径会按仓库根解析，``--out-dir`` 指向仓库外就写错位置。"""
    batch_db = tmp_path / "sub" / "trace.db"
    settings = rb.build_settings(batch_db, read_timeout=10.0)
    assert settings.sqlite_path == batch_db.resolve()
    assert settings.sqlite_path.is_absolute()


# ---------------------------------------------------------------- 单号码落盘


class _StubScheduler:
    """写入一行 running，可选地卡住一段时间，然后按状态收尾。

    先建行再卡住是照真实行为来的：真实运行在开跑时就写下 running 行，超时中断时那一行
    已经在库里。若先卡住再建行，超时后就没有任何留痕可导出——那会把「导出逻辑坏了」与
    「还没来得及写库」混成一回事。
    """

    def __init__(self, store: Store, *, delay: float = 0.0, status: str = RunStatus.SUCCEEDED):
        self.store = store
        self.delay = delay
        self.status = status
        self.calls: list[dict] = []

    async def run(self, request):
        self.calls.append(
            {
                "run_id": request.run_id,
                "inputs": dict(request.inputs),
                "business_ref": request.business_ref,
            }
        )
        await self.store.enqueue_run(
            RunRecordRow(
                run_id=request.run_id,
                workflow_id=request.workflow.workflow_id,
                status=RunStatus.RUNNING,
                inputs=dict(request.inputs),
                business_ref=request.business_ref,
            )
        )
        await self.store.upsert_node_execution(
            run_id=request.run_id,
            node_id="end_node",
            node_title="输出",
            node_type="end",
            call_path="",
            status=RunStatus.SUCCEEDED,
            inputs={"phone_number": request.inputs.get("phone_number", "")},
            outputs={"result1": "中文结果：画像摘要"},
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        outputs = {"result1": "中文结果：画像摘要"} if self.status == RunStatus.SUCCEEDED else {}
        await self.store.update_run_finished(
            request.run_id,
            status=self.status,
            outputs=outputs,
            duration_ms=42,
        )
        return _Outcome(request.run_id, self.status, outputs)


class _Outcome:
    def __init__(self, run_id: str, status: str, outputs: dict):
        self.run_id = run_id
        self.status = status
        self.outputs = outputs
        self.error = None
        self.duration_ms = 42
        self.node_executions: list = []
        self.sub_run_ids: list = []


class _StubService:
    """``run_one`` 只需要 ``require_workflow`` 与 ``scheduler`` 两件东西。"""

    def __init__(self, store: Store, workflow, scheduler):
        self.store = store
        self.scheduler = scheduler
        self._workflow = workflow

    def require_workflow(self, workflow_id: str):
        return self._workflow


async def _stubbed(tmp_path, *, delay: float = 0.0, status: str = RunStatus.SUCCEEDED):
    from customer_profile.workflows import customer_profile_entry as entry

    settings = make_settings(tmp_path)
    store = Store(settings.sqlite_path)
    store.connect()
    scheduler = _StubScheduler(store, delay=delay, status=status)
    service = _StubService(store, entry.WORKFLOW, scheduler)
    return service, scheduler, store


async def test_run_one_archives_four_files(tmp_path):
    """单号码跑完落四个产物文件，且入参恒为空 batch_id。"""
    service, scheduler, store = await _stubbed(tmp_path)
    batch_dir = tmp_path / "batch"
    result = await rb.run_one(
        service,
        PHONE,
        timeout=30.0,
        policy=ba.ExportPolicy(),
        batch_dir=batch_dir,
    )

    assert result.status == RunStatus.SUCCEEDED
    assert result.exported is True
    assert result.bytes_written > 0
    assert result.export_summary["node_count"] == 1

    target = rb.phone_artifact_dir(batch_dir, PHONE)
    for name in ba.EXPORT_FILENAMES:
        assert (target / name).is_file(), name
    payload = json.loads((target / "result.json").read_text(encoding="utf-8"))
    assert payload["result1"] == "中文结果：画像摘要"
    assert payload["outputs"]["result1"] == "中文结果：画像摘要"

    # 入参口径：batch_id 恒为空串（它会被透传进 result1），业务标识走 business_ref
    call = scheduler.calls[0]
    assert call["inputs"] == {"phone_number": PHONE, "batch_id": ""}
    assert call["business_ref"] == PHONE
    store.close()


async def test_run_one_records_failure_without_raising(tmp_path):
    """节点/运行失败不抛异常——批次要继续跑下一个号码。"""
    service, _, store = await _stubbed(tmp_path, status=RunStatus.FAILED)
    result = await rb.run_one(
        service, PHONE, timeout=30.0, policy=ba.ExportPolicy(),
        batch_dir=tmp_path / "batch",
    )
    assert result.status == RunStatus.FAILED
    assert result.exported is True  # 失败的运行也要归档，否则看不到卡在哪
    store.close()


async def test_run_one_times_out_and_still_exports(tmp_path):
    """超时记为 timeout，且**仍然导出**已完成的留痕——半棵树比没有强。"""
    service, _, store = await _stubbed(tmp_path, delay=0.5)
    result = await rb.run_one(
        service, PHONE, timeout=0.05, policy=ba.ExportPolicy(),
        batch_dir=tmp_path / "batch",
    )
    assert result.status == "timeout"
    assert "超时" in (result.error or "")
    assert result.exported is True
    store.close()


async def test_run_one_reports_export_failure_instead_of_losing_it(tmp_path):
    """导出失败要写进结果，不静默吞掉。"""
    service, _, store = await _stubbed(tmp_path)
    store.close()  # 关掉连接，导出必然失败
    result = await rb.run_one(
        service, PHONE, timeout=30.0, policy=ba.ExportPolicy(),
        batch_dir=tmp_path / "batch",
    )
    assert result.exported is False
    assert "导出失败" in (result.error or "")


# ---------------------------------------------------------------- 汇总


def test_summary_reports_failures_with_rerun_command(tmp_path):
    """汇总把失败号码单列，并给出可直接复制的补跑命令。"""
    import run_baseline as rb2

    results = [
        rb2.PhoneResult(phone="13415100087", status=RunStatus.SUCCEEDED, duration_ms=10),
        rb2.PhoneResult(
            phone="13430349943", status=RunStatus.FAILED, error="模型超时 | \n换行"
        ),
    ]
    written = rb2.write_summary(tmp_path, results, "20260924T1530Z-abcdef12")
    assert written["summary.md"] > 0

    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "13430349943" in text
    assert "--only 13430349943" in text
    assert "|" in text  # 表格形态
    assert "\n换行" not in text or "换行" in text  # 竖线与换行被清洗，表格不被破坏

    machine = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert machine["phone_count"] == 2
    assert machine["succeeded"] == 1


def test_merge_results_keeps_prior_phones(tmp_path):
    """补跑时汇总必须仍是**整批**的视图，先前的号码不能消失。

    这条挡的是「汇总只反映最后一次调用」——那会让「这批跑了什么」取决于最后一次调用，
    批次记录随之失去意义。产物目录是事实来源，合并从磁盘读。
    """
    for phone in ("13415100087", "13430349943"):
        _write_result(tmp_path, phone, RunStatus.SUCCEEDED)

    fresh = [rb.PhoneResult(phone="13527911093", status=RunStatus.SUCCEEDED)]
    merged = rb.merge_results(tmp_path, fresh)
    assert [item.phone for item in merged] == [
        "13415100087",
        "13430349943",
        "13527911093",
    ]


def test_merge_results_prefers_fresh_over_disk(tmp_path):
    """同号码的本次结果覆盖旧记录——重跑之后应以新结果为准。"""
    _write_result(tmp_path, "13415100087", RunStatus.FAILED)
    fresh = [rb.PhoneResult(phone="13415100087", status=RunStatus.SUCCEEDED)]
    merged = rb.merge_results(tmp_path, fresh)
    assert len(merged) == 1
    assert merged[0].status == RunStatus.SUCCEEDED


def test_load_batch_results_reads_artifacts_from_disk(tmp_path):
    """从产物重建状态：状态、run_id、字节数都来自磁盘，不依赖内存。"""
    _write_result(tmp_path, "13415100087", RunStatus.SUCCEEDED)
    loaded = rb.load_batch_results(tmp_path)
    assert set(loaded) == {"13415100087"}
    item = loaded["13415100087"]
    assert item.status == RunStatus.SUCCEEDED
    assert item.bytes_written > 0
    assert item.exported is True


def test_load_batch_results_skips_corrupt_artifacts(tmp_path):
    """损坏的产物跳过而不是让整批汇总读不出来。"""
    _write_result(tmp_path, "13415100087", RunStatus.SUCCEEDED)
    bad = rb.phone_artifact_dir(tmp_path, "13430349943")
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "result.json").write_text("{not json", encoding="utf-8")
    assert set(rb.load_batch_results(tmp_path)) == {"13415100087"}


def test_batch_bytes_on_disk_counts_the_whole_batch(tmp_path):
    """体积按目录累计，不只算本次写入——补跑时「本次」不代表批次总量。"""
    _write_result(tmp_path, "13415100087", RunStatus.SUCCEEDED)
    first = rb.batch_bytes_on_disk(tmp_path)
    assert first > 0
    _write_result(tmp_path, "13430349943", RunStatus.SUCCEEDED)
    assert rb.batch_bytes_on_disk(tmp_path) > first


def test_no_op_run_leaves_batch_files_untouched(tmp_path):
    """对已完成的批次重跑同一个 ``--only`` 时，批次文件**一个字节都不能变**。

    这条挡的是一个真实踩到过的缺陷：空队列分支此前会无条件重写 manifest，把
    ``created_at`` 重置成本次调用时刻、并丢掉 ``finished_at`` / ``bytes_written``。
    症状隐蔽——汇总（summary.json/md）当时是对的，只有 manifest 被悄悄改掉。

    用 subprocess 走真实脚本：这条路径在 ``build_service`` 之前就返回，不出网。
    """
    import subprocess as sp
    import sys as _sys

    batch_dir = tmp_path / "batches" / "b-noop"
    target = rb.phone_artifact_dir(batch_dir, PHONE)
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text(
        json.dumps({"phone": PHONE, "status": RunStatus.SUCCEEDED}, ensure_ascii=False),
        encoding="utf-8",
    )
    (batch_dir / "phones.txt").write_text(PHONE + "\n", encoding="utf-8")
    # 造一份「已完成批次」应有的汇总与 manifest
    batch_dir.joinpath("summary.json").write_text(
        json.dumps({"batch_id": "b-noop", "phone_count": 1, "succeeded": 1}),
        encoding="utf-8",
    )
    batch_dir.joinpath("summary.md").write_text("# 已完成\n", encoding="utf-8")
    batch_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "batch_id": "b-noop",
                "created_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T01:00:00+00:00",
                "queue_this_run": [PHONE],
                "bytes_written": 12345,
            }
        ),
        encoding="utf-8",
    )

    before = {
        name: (batch_dir / name).read_bytes()
        for name in ("summary.json", "summary.md", "manifest.json")
    }

    result = sp.run(
        [
            _sys.executable,
            str(REPO_ROOT / "scripts" / "run_baseline.py"),
            "--batch-id",
            "b-noop",
            "--out-dir",
            str(tmp_path),
            "--only",
            PHONE,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    for name, payload in before.items():
        assert (batch_dir / name).read_bytes() == payload, f"{name} 被改动了"


def test_manifest_is_loaded_for_history_and_created_at(tmp_path):
    """补跑要继承批次的**开批时刻**与**入队历史**，不把它们改成补跑时的情况。"""
    path = tmp_path / "manifest.json"
    assert rb.load_manifest(path) == {}  # 不存在时安全回落

    path.write_text(
        json.dumps(
            {
                "created_at": "2026-01-01T00:00:00+00:00",
                "runs_this_invocation": [{"at": "t1", "queued": ["a"]}],
            }
        ),
        encoding="utf-8",
    )
    loaded = rb.load_manifest(path)
    assert loaded["created_at"] == "2026-01-01T00:00:00+00:00"
    assert loaded["runs_this_invocation"] == [{"at": "t1", "queued": ["a"]}]

    path.write_text("{broken", encoding="utf-8")
    assert rb.load_manifest(path) == {}  # 损坏时也回落，不让补跑整条失败


def test_concurrency_is_capped_at_four():
    """并发上限是 4：用户口径「4 并发之内」，脚本不给更高档位。"""
    assert rb.MAX_CONCURRENCY == 4
    assert rb.DEFAULT_CONCURRENCY == 4
    with pytest.raises(SystemExit):
        rb.main(["--concurrency", "5", "--limit", "0", "--only", ""])


def test_gate_widens_with_concurrency():
    """随并发放宽的出网位必须大于并发数，否则并发收益被闸门吃掉。"""
    assert rb.GATE_FOR_CONCURRENCY > rb.MAX_CONCURRENCY

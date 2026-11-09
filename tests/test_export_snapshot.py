"""按需取数：时间窗/号码筛选、子运行连带、定义快照连带与已有文件保护。

不跑工作流，直接用合成数据往临时库里写四张表——取数脚本的职责是「选哪些行、怎么搬」，
与工作流逻辑无关。合成数据能精确控制「子运行是否被连带」「无关运行是否被排除」。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from customer_profile.persistence.schema import SCHEMA_STATEMENTS

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import export_snapshot as es  # noqa: E402


def _make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    now = 1_760_000_000_000
    # definition_version_id 从 1 开始（AUTOINCREMENT），0 不是合法版本号。
    runs = [
        ("root-a", None, "13800000000", 1, now),
        ("child-a", "root-a", "13800000000", 1, now),
        ("grand-a", "child-a", "13800000000", 1, now),
        ("root-b", None, "13900000001", 2, now + 86_400_000),
        ("stale", None, "13700000002", 3, now - 30 * 86_400_000),
    ]
    for run_id, parent, ref, version, created in runs:
        connection.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, parent_run_id, business_ref, status, "
            " definition_version_id, created_at_ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, "demo", parent, ref, "succeeded", version, created),
        )
        connection.execute(
            "INSERT INTO node_executions "
            "(run_id, node_id, node_type, status) VALUES (?,?,?,?)",
            (run_id, "n1", "template-transform", "succeeded"),
        )
    for version in (0, 1, 2):
        connection.execute(
            "INSERT INTO definition_versions "
            "(workflow_id, definition_hash, definition_json, created_at_ms) "
            "VALUES (?,?,?,?)",
            ("demo", f"hash-{version}", "{}", now),
        )
    for phone, level in (("13800000000", "B"), ("13900000001", "A")):
        connection.execute(
            "INSERT INTO profile_baselines "
            "(phone, compat_key, updated_at_ms, deal_level, consumed_keys_json, callback_json) "
            "VALUES (?,?,?,?,?,?)",
            (phone, "ck-1", now, level, '["k1"]',
             '{"phone":"%s","batch_id":"old","analysis_result":{"profile_summary":{}}}' % phone),
        )
    connection.commit()
    connection.close()


def _export(tmp_path: Path, **kwargs) -> tuple[dict, Path]:
    source = tmp_path / "source.db"
    _make_db(source)
    out_dir = tmp_path / "out"
    params = {"since_ms": None, "until_ms": None, "business_refs": None}
    params.update(kwargs)
    manifest = es.export(source, out_dir, "snap", **params)
    return manifest, out_dir / "snap.db"


def _run_ids(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT run_id FROM workflow_runs")}
    finally:
        connection.close()


def test_no_filter_copies_everything(tmp_path):
    manifest, target = _export(tmp_path)
    assert manifest["counts"]["workflow_runs"] == 5
    assert manifest["counts"]["node_executions"] == 5
    assert manifest["counts"]["definition_versions"] == 3
    assert _run_ids(target) == {"root-a", "child-a", "grand-a", "root-b", "stale"}


def test_phone_filter_takes_whole_tree(tmp_path):
    manifest, target = _export(tmp_path, since_ms=None, until_ms=None, business_refs=["13800000000"])
    assert manifest["counts"]["workflow_runs"] == 3
    assert _run_ids(target) == {"root-a", "child-a", "grand-a"}


def test_time_window_excludes_old_runs(tmp_path):
    manifest, target = _export(
        tmp_path, since_ms=1_760_000_000_000 - 1, until_ms=None, business_refs=None
    )
    assert "stale" not in _run_ids(target)
    assert manifest["counts"]["workflow_runs"] == 4


def test_definition_snapshot_follows_referenced_runs(tmp_path):
    manifest, target = _export(tmp_path, since_ms=None, until_ms=None, business_refs=["13800000000"])
    assert manifest["counts"]["definition_versions"] == 1


def test_refuses_to_overwrite(tmp_path):
    _export(tmp_path)
    source = tmp_path / "source.db"
    source2 = tmp_path / "source2.db"
    _make_db(source2)
    with pytest.raises(SystemExit):
        es.export(source2, tmp_path / "out", "snap", None, None, None)


def test_target_has_expected_indexes(tmp_path):
    _, target = _export(tmp_path)
    connection = sqlite3.connect(target)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()
    assert "idx_runs_business" in names
    assert "idx_attempts_run" in names


def test_baselines_travel_with_the_snapshot(tmp_path):
    """基线表按号码连带：本地起服务才能读到上一版、跑出 T0。

    这张表不挂在任何 run 上——漏搬不会报错，只会让本地每一轮都退化成全量。
    """
    manifest, target = _export(tmp_path)
    assert manifest["counts"]["profile_baselines"] == 2
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT COUNT(*) FROM profile_baselines").fetchone()[0] == 2
    finally:
        connection.close()


def test_baseline_filter_follows_phone_list(tmp_path):
    manifest, target = _export(tmp_path, since_ms=None, until_ms=None, business_refs=["13800000000"])
    assert manifest["counts"]["profile_baselines"] == 1
    connection = sqlite3.connect(target)
    try:
        phones = {row[0] for row in connection.execute("SELECT phone FROM profile_baselines")}
    finally:
        connection.close()
    assert phones == {"13800000000"}


def test_snapshot_baseline_is_readable_by_the_repository(tmp_path):
    """搬过去之后要能被真实仓储读出来：列对不上时不报错，只是永远读不到基线。"""
    import asyncio

    from customer_profile.persistence.profile_baselines import BaselineRepository
    from customer_profile.persistence.store import Store

    _, target = _export(tmp_path)
    store = Store(target)
    store.connect()

    async def _load():
        try:
            return await BaselineRepository(store).load("13800000000")
        finally:
            store.close()

    baseline = asyncio.run(_load())
    assert baseline is not None
    assert baseline.deal_level == "B"
    assert baseline.consumed_keys == ("k1",)


def test_source_without_baseline_table_still_exports(tmp_path):
    """旧库没有这张表：返回 0 而不是报错，那是「没发生过增量」。"""
    source = tmp_path / "old.db"
    connection = sqlite3.connect(source)
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute("DROP TABLE profile_baselines")
    connection.execute(
        "INSERT INTO workflow_runs (run_id, workflow_id, status, created_at_ms) "
        "VALUES ('r1','demo','succeeded',1)"
    )
    connection.commit()
    connection.close()

    manifest = es.export(source, tmp_path / "out2", "old", None, None, None)
    assert manifest["counts"]["profile_baselines"] == 0
    assert manifest["format"] >= 3


def test_parse_time_end_of_day_is_exclusive_next_midnight():
    start = es.parse_time("2026-10-20")
    end = es.parse_time("2026-10-20", end_of_day=True)
    assert end - start == 86_400_000


def test_parse_time_rejects_garbage():
    with pytest.raises(SystemExit):
        es.parse_time("not-a-date")

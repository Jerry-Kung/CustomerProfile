"""按案取包：定位、复制、解包，以及三种「找不到」各自的说明。

第三种情形最要紧：包不存在时必须说清是「过期删了」「从未打包」还是「自己没清理过」，
否则排障的人会把「取不到」当成「这次运行本来就没数据」。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import gzip

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import fetch_run_archive as fra  # noqa: E402
from customer_profile.persistence.schema import SCHEMA_STATEMENTS  # noqa: E402

NOW = 1_762_300_000_000


def _package(payload: dict) -> bytes:
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode())


def _make_db(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for run_id, parent, ref, state, created in rows:
            connection.execute(
                "INSERT INTO workflow_runs (run_id, workflow_id, parent_run_id, "
                " business_ref, status, payload_state, created_at_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, "demo", parent, ref, "succeeded", state, created),
            )
        connection.commit()
    finally:
        connection.close()


def _day(ms: int) -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "trace.db"
    archives = tmp_path / "trace-archives"
    _make_db(
        db,
        [
            ("root-a", None, "cdp:13800000000:B1", "packed", NOW),
            ("child-a", "root-a", None, "packed", NOW),
            ("root-b", None, "cdp:13900000001:B1", "packed", NOW),
            ("root-c", None, "cdp:13700000002:B1", "purged", NOW),
            ("root-d", None, "cdp:13600000003:B1", None, NOW),
        ],
    )
    for run_id in ("root-a", "root-b"):
        target = archives / _day(NOW) / f"{run_id}.json.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            _package(
                {
                    "manifest": {
                        "package_version": 1,
                        "persist_mode": "high",
                        "root_run_id": run_id,
                        "counts": {"runs": 1, "nodes": 2, "attempts": 1},
                        "timestamps": {"created_at": "x", "finished_at": "y"},
                        "media": {"url_may_expire": False},
                        "unresolved_media": 0,
                    },
                    "runs": [],
                    "nodes": [],
                    "attempts": [],
                    "definitions": {},
                }
            )
        )
    return db, archives


# ---------------------------------------------------------------- 定位


def test_run_id_resolves_to_root(env):
    db, archives = env
    result = fra.fetch(db, archives, run_id="child-a")
    assert [entry["run_id"] for entry in result["fetched"]] == ["root-a"]


def test_phone_matches_business_ref(env):
    db, archives = env
    result = fra.fetch(db, archives, phone="13900000001")
    assert [entry["run_id"] for entry in result["fetched"]] == ["root-b"]


def test_phone_with_time_window_excludes_others(env):
    db, archives = env
    result = fra.fetch(
        db, archives, phone="137", since_ms=NOW + 86_400_000
    )
    assert result["fetched"] == []


# ---------------------------------------------------------------- 取回


def test_copy_writes_same_bytes(env, tmp_path):
    db, archives = env
    out = tmp_path / "out"
    result = fra.fetch(db, archives, run_id="root-a", out_dir=out)
    copied = Path(result["fetched"][0]["copied"])
    assert copied.exists()
    assert copied.read_bytes() == (archives / _day(NOW) / "root-a.json.gz").read_bytes()


def test_unpack_writes_readable_json(env, tmp_path):
    db, archives = env
    out = tmp_path / "out"
    result = fra.fetch(db, archives, run_id="root-a", out_dir=out, unpack=True)
    unpacked = Path(result["fetched"][0]["unpacked"])
    payload = json.loads(unpacked.read_text(encoding="utf-8"))
    assert payload["manifest"]["root_run_id"] == "root-a"


# ---------------------------------------------------------------- 找不到


def test_purged_is_explained_as_never_packaged(env):
    db, archives = env
    result = fra.fetch(db, archives, run_id="root-c")
    assert result["fetched"] == []
    assert "存量清理" in result["missing"][0]


def test_unpacked_run_is_distinguished(env):
    db, archives = env
    result = fra.fetch(db, archives, run_id="root-d")
    assert "从未打包" in result["missing"][0]


def test_missing_file_mentions_retention(env, tmp_path):
    db, archives = env
    (archives / _day(NOW) / "root-a.json.gz").unlink()
    result = fra.fetch(db, archives, run_id="root-a")
    assert "保留期" in result["missing"][0]


def test_cli_exit_code_nonzero_when_missing(env, capsys):
    db, archives = env
    code = fra.main(["--db", str(db), "--archive-dir", str(archives), "--run-id", "root-c"])
    assert code == 3


def test_cli_exit_code_zero_when_fetched(env):
    db, archives = env
    code = fra.main(["--db", str(db), "--archive-dir", str(archives), "--run-id", "root-a"])
    assert code == 0


def test_cli_requires_a_locator(env):
    db, archives = env
    assert fra.main(["--db", str(db), "--archive-dir", str(archives)]) == 2

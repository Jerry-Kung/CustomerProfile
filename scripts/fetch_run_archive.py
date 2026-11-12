#!/usr/bin/env python
"""按 run_id 或号码 + 时间窗取回留痕包，供排障用。

## 它解决什么问题

一台运行台看到某次运行有问题，想把它拿到本地细看。整库快照（``export_snapshot.py``）
动辄上百 MB、还依赖内网主机；而这一案的全部运行时数据其实就在**一个包**里。

## 两条口径

1. **包是唯一副本。** 打包成功后库里的载荷列已被清空，取不到包就说明数据已经没了。
   因此找不到包时**非零退出**并说明原因（超过保留期 / 从未生成 / 目录指错），
   而不是安静地打印一行「未找到」。
2. **默认只复制，不改变源目录。** 想直接看内容再加 ``--unpack``；源目录是证据，
   不在这里就地改动。

## 源库可以是 SQLite 或 MySQL

骨架库有两处可能：本地/批次/快照是 SQLite 文件，UAT/生产是 MySQL。
用 ``--db`` 指文件，或用 ``--source-url mysql://…`` 直连运行时库。**包的位置不受影响**
（包始终在 ``DATA_DIR/trace-archives`` 下），源库只用来定位「这次运行属于哪棵树」。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.persistence import archive  # noqa: E402

DEFAULT_ARCHIVE_DIR = REPO_ROOT / "data" / "trace-archives"

Source = "Path | str"
"""``Path``（SQLite 文件）或 ``mysql://…`` 形式的 DSN。"""


def _is_mysql(source: Any) -> bool:
    return isinstance(source, str) and source.startswith("mysql://")


def _connect(source: Any):
    """打开源库。两种后端都返回「行可按列名取」的连接。"""
    if _is_mysql(source):
        from customer_profile.persistence.dbapi import MySqlBackend, build_backend

        backend = build_backend(source)
        if not isinstance(backend, MySqlBackend):
            raise ValueError(f"不是 MySQL 连接串：{source}")
        return backend.connect()

    import sqlite3

    connection = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """统一的取值：两种驱动都归一成 ``list[dict]``。"""
    rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _roots_by_id(source: Any, run_id: str) -> list[dict]:
    """给定 run_id，返回所属**根运行**的 (run_id, payload_state, created_at_ms)。"""
    connection = _connect(source)
    try:
        rows = _rows(
            connection,
            "WITH RECURSIVE up(run_id, parent_run_id) AS ("
            " SELECT run_id, parent_run_id FROM workflow_runs WHERE run_id = ?"
            " UNION ALL SELECT p.run_id, p.parent_run_id FROM workflow_runs p"
            " JOIN up ON p.run_id = up.parent_run_id) "
            "SELECT run_id FROM up WHERE parent_run_id IS NULL LIMIT 1",
            (run_id,),
        )
        if not rows:
            return []
        return _roots_by_ids(connection, [rows[0]["run_id"]])
    finally:
        connection.close()


def _roots_by_phone(
    source: Any, phone: str, since_ms: int | None, until_ms: int | None
) -> list[dict]:
    connection = _connect(source)
    try:
        clauses = ["parent_run_id IS NULL", "business_ref LIKE ?"]
        params: list = [f"%{phone}%"]
        if since_ms is not None:
            clauses.append("created_at_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("created_at_ms < ?")
            params.append(until_ms)
        rows = _rows(
            connection,
            "SELECT run_id FROM workflow_runs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at_ms",
            params,
        )
        return _roots_by_ids(connection, [row["run_id"] for row in rows])
    finally:
        connection.close()


def _roots_by_ids(connection: Any, run_ids: Sequence[str]) -> list[dict]:
    result = []
    for run_id in run_ids:
        rows = _rows(
            connection,
            "SELECT payload_state, created_at_ms FROM workflow_runs WHERE run_id = ?",
            (run_id,),
        )
        if not rows:
            continue
        result.append(
            {
                "run_id": run_id,
                "payload_state": rows[0]["payload_state"],
                "created_at_ms": rows[0]["created_at_ms"],
            }
        )
    return result


def _explain_missing(root: dict, archive_dir: Path) -> str:
    """包找不到时说明是哪一种找不到——这三种原因的处置完全不同。"""
    state = root.get("payload_state")
    day = archive._archive_day(root.get("created_at_ms"))
    target = archive_dir / day / f"{root['run_id']}.json.gz"
    if state == "purged":
        return f"{root['run_id']}：存量清理过的运行，本来就没有包"
    if state != "packed":
        return (
            f"{root['run_id']}：该运行从未打包（payload_state={state!r}），"
            "可能仍在库里或从未收尾"
        )
    return (
        f"{root['run_id']}：已打包但文件不存在，预期路径 {target}；"
        "多为超过保留期被删，或 --archive-dir 指向了别处"
    )


def fetch(
    source: Any,
    archive_dir: Path,
    *,
    run_id: str | None = None,
    phone: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    out_dir: Path | None = None,
    unpack: bool = False,
) -> dict:
    """``source`` 是 SQLite 文件路径，或 ``mysql://…``。"""
    if run_id is not None:
        roots = _roots_by_id(source, run_id)
    elif phone is not None:
        roots = _roots_by_phone(source, phone, since_ms, until_ms)
    else:
        raise ValueError("需要 --run-id 或 --phone")

    fetched: list[dict] = []
    missing: list[str] = []
    for root in roots:
        day = archive._archive_day(root.get("created_at_ms"))
        source = archive_dir / day / f"{root['run_id']}.json.gz"
        if not source.exists():
            missing.append(_explain_missing(root, archive_dir))
            continue
        entry = {"run_id": root["run_id"], "source": str(source)}
        if out_dir is not None:
            target_dir = Path(out_dir) / root["run_id"]
            target_dir.mkdir(parents=True, exist_ok=True)
            if unpack:
                package = archive.read_package(source)
                target = target_dir / "package.json"
                target.write_text(
                    json.dumps(package, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                    newline="\n",
                )
                entry["unpacked"] = str(target)
                entry["manifest"] = package.get("manifest")
            else:
                target = target_dir / source.name
                shutil.copy2(source, target)
                entry["copied"] = str(target)
        else:
            entry["manifest"] = archive.read_package(source).get("manifest")
        fetched.append(entry)
    return {"fetched": fetched, "missing": missing, "roots": len(roots)}


def _parse_time(raw: str | None) -> int | None:
    if not raw:
        return None
    day = datetime.fromisoformat(raw)
    return int(day.timestamp() * 1000)


def _print_manifest(manifest: dict) -> None:
    times = manifest.get("timestamps") or {}
    print(
        f"    mode={manifest.get('persist_mode')} "
        f"root={manifest.get('root_run_id')} "
        f"counts={manifest.get('counts')}"
    )
    print(
        f"    created={times.get('created_at')} finished={times.get('finished_at')} "
        f"url_may_expire={((manifest.get('media') or {}).get('url_may_expire'))} "
        f"unresolved_media={manifest.get('unresolved_media')}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 run_id 或号码取回留痕包")
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "customer_profile.db"),
                        help="源 SQLite 骨架库路径")
    parser.add_argument(
        "--source-url",
        default="",
        help="源 MySQL DSN（mysql://…）。给了它就不读 --db；包目录位置不变",
    )
    parser.add_argument("--archive-dir", default=None, help="包目录；缺省 <db 同级>/trace-archives")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--phone", default=None, help="号码；按 business_ref 模糊匹配")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", default=None, help="取回的目标目录")
    parser.add_argument("--unpack", action="store_true", help="解包成 json 而不是复制")
    args = parser.parse_args(argv)

    if args.source_url.startswith("mysql://"):
        # MySQL 源没有「库文件同级目录」这个概念，包目录必须显式给或走缺省仓库路径。
        source: Any = args.source_url
        archive_dir = (
            Path(args.archive_dir) if args.archive_dir else DEFAULT_ARCHIVE_DIR
        )
    else:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"ERROR: 库不存在 {db_path}", file=sys.stderr)
            return 2
        source = db_path
        archive_dir = (
            Path(args.archive_dir) if args.archive_dir else db_path.parent / "trace-archives"
        )

    if args.run_id is None and args.phone is None:
        print("ERROR: 需要 --run-id 或 --phone", file=sys.stderr)
        return 2

    result = fetch(
        source,
        archive_dir,
        run_id=args.run_id,
        phone=args.phone,
        since_ms=_parse_time(args.since),
        until_ms=_parse_time(args.until),
        out_dir=Path(args.out_dir) if args.out_dir else None,
        unpack=args.unpack,
    )
    for entry in result["fetched"]:
        print(f"OK {entry['run_id']}")
        if entry.get("manifest"):
            _print_manifest(entry["manifest"])
        if entry.get("copied"):
            print(f"    -> {entry['copied']}")
        if entry.get("unpacked"):
            print(f"    -> {entry['unpacked']}")
    for line in result["missing"]:
        print(f"MISS {line}", file=sys.stderr)
    if not result["roots"]:
        print("ERROR: 没有匹配的运行", file=sys.stderr)
        return 2
    return 3 if result["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

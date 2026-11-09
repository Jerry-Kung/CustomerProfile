"""按需取数：把 UAT 留痕库的一部分拷成自包含的快照文件，供内网外交接。

**为什么是「拷库」而不是「调接口导 JSON」。** 运行台的展示依赖四张表之间的定位键
（``run_id``、``(node_id, call_path)``、``definition_version_id``）。只要这四张表连同
定义快照一起搬走，本地把 ``DATABASE_URL`` 指向快照、起 API，运行台就能完整复现当时的
图、节点与每次请求——恢复侧因此**不需要任何代码**。导出成 JSON 反而要另写一个恢复器。

**一致性靠一个读事务，不靠停服。** 库是 WAL 模式，拷贝期间持有 ``BEGIN`` 读事务，
拿到的就是同一时刻的快照；期间新写入不阻塞、也不会混进来。因此**不要**直接拷 ``.db``
文件本身——WAL 里尚未合并的最近写入会丢。

**筛选粒度是「根运行」。** 时间窗与号码只用来挑根运行；它的整棵子运行树（子流程、
迭代各轮）一律连带搬走，否则拿到的运行在本地无法下钻。

用法：

    python scripts/export_snapshot.py --out-dir D:/tmp/uat-1
    python scripts/export_snapshot.py --out-dir D:/tmp/uat-1 \\
        --since 2026-10-20 --until 2026-10-21 --phone 13800000000,13900000001
    python scripts/export_snapshot.py --out-dir D:/tmp/uat-1 --dry-run
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "customer_profile.db"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "data" / "trace-archives"

TABLES = ("workflow_runs", "node_executions", "request_attempts", "definition_versions")
"""按依赖顺序排列：运行 → 节点/尝试 → 定义快照。"""

BASELINE_TABLE = "profile_baselines"
"""增量基线表：按手机号一行，**不挂在任何 run 上**。

必须随快照一起带走。少了它，本地起服务跑增量入口时读不到基线，每一轮都会退化成
T3（全量重建）——产物照样出得来，于是会被误判成「增量没生效」而不是「快照不全」
（``03`` P3 的排期约束正是为此）。
"""

DEFAULT_TZ = timezone(timedelta(hours=8))
"""不带时区的时间按业务时区（东八区）解释。内网 UAT 上打的时间通常就是本地时间。"""

CHUNK = 500
"""``IN (?)`` 的参数上限远高于此；分块是为了让内存占用与选中运行数无关。"""

FORMAT_VERSION = 3
"""3：连带 ``profile_baselines``。2 及更早的快照跑不出增量，只能复现 T3。"""


# ---------------------------------------------------------------- 时间


def parse_time(raw: str | None, *, end_of_day: bool = False) -> int | None:
    """把命令行时间解析成毫秒时间戳。支持 ``YYYY-MM-DD`` 与 ISO 8601。

    只写日期的 ``--until`` 按**当天结束**理解（右开区间取次日零点），否则
    ``--until 2026-10-20`` 会把这一天整天的运行全排除掉，与直觉相反。
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            moment = datetime(day.year, day.month, day.day, tzinfo=DEFAULT_TZ)
            if end_of_day:
                moment += timedelta(days=1)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=DEFAULT_TZ)
    except ValueError as exc:
        raise SystemExit(f"cannot parse time {raw!r}: {exc}") from exc
    return int(moment.timestamp() * 1000)


# ---------------------------------------------------------------- 挑选


def select_root_runs(
    connection: sqlite3.Connection,
    since_ms: int | None,
    until_ms: int | None,
    business_refs: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """选中根运行的 (run_id, payload_state, created_at_ms)。

    包按**根运行**打，所以取数也要按根运行决定「这棵树需不需要带包」。
    """
    where = ["parent_run_id IS NULL"]
    params: list[Any] = []
    if since_ms is not None:
        where.append("created_at_ms >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("created_at_ms < ?")
        params.append(until_ms)
    if business_refs:
        marks = ",".join("?" for _ in business_refs)
        where.append(f"business_ref IN ({marks})")
        params.extend(business_refs)
    rows = connection.execute(
        f"SELECT run_id, payload_state, created_at_ms FROM workflow_runs "
        f"WHERE {' AND '.join(where)} ORDER BY created_at_ms",
        params,
    )
    # 按位置取而不是 ``dict(row)``：调用方的连接不一定设了 ``row_factory``，
    # 依赖它会让本函数在测试里直接抛 ValueError。
    return [
        {"run_id": row[0], "payload_state": row[1], "created_at_ms": row[2]}
        for row in rows
    ]


def select_runs(
    connection: sqlite3.Connection,
    since_ms: int | None,
    until_ms: int | None,
    business_refs: Sequence[str] | None,
) -> list[str]:
    """返回选中根运行及其全部后代运行 ID。"""
    where = ["parent_run_id IS NULL"]
    params: list[Any] = []
    if since_ms is not None:
        where.append("created_at_ms >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("created_at_ms < ?")
        params.append(until_ms)
    if business_refs:
        marks = ",".join("?" for _ in business_refs)
        where.append(f"business_ref IN ({marks})")
        params.extend(business_refs)
    sql = f"""
        WITH RECURSIVE selected(run_id) AS (
            SELECT run_id FROM workflow_runs WHERE {' AND '.join(where)}
            UNION
            SELECT r.run_id FROM workflow_runs r
              JOIN selected s ON r.parent_run_id = s.run_id
        )
        SELECT run_id FROM selected
    """
    return [row[0] for row in connection.execute(sql, params)]


def _chunks(values: Sequence[str], size: int = CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------- 拷贝


def create_target(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    """按源库的建表语句建目标库。

    直接照抄 ``sqlite_master`` 而不是复用 ``persistence.schema``：后者是「当前代码的
    期望结构」，而既有库里经 ``_apply_migrations`` 补出来的列（``anomaly`` 等）不在其中。
    快照要的是**当时的结构**，抄源库才不会漏列。
    """
    statements = [
        row[0]
        for row in source.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type IN ('table','index') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for statement in statements:
        target.execute(statement)


def copy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    where: str = "",
    params: Sequence[Any] = (),
) -> int:
    """把 ``table`` 中满足 ``where`` 的行原样搬过去，返回行数。

    列取源表与目标表的交集：源库若比目标多/少一列（脚本与库版本不同步），
    交集拷贝仍能进行，并在返回值上如实反映行数。
    """
    source_columns = _columns(source, table)
    target_columns = set(_columns(target, table))
    columns = [name for name in source_columns if name in target_columns]
    if not columns:
        raise SystemExit(f"table {table} missing in target; cannot build snapshot")
    select = f"SELECT {', '.join(columns)} FROM {table}"
    if where:
        select += f" WHERE {where}"
    select += " ORDER BY rowid"
    insert = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    count = 0
    cursor = source.execute(select, params)
    while True:
        batch = cursor.fetchmany(CHUNK)
        if not batch:
            break
        target.executemany(insert, batch)
        count += len(batch)
    return count


def copy_archives(
    roots: Sequence[Mapping[str, Any]],
    source_archive_dir: Path | None,
    target_dir: Path,
) -> dict[str, Any]:
    """把选中根部里已打包的包复制出来，逐个校验 sha256。

    包是运行时数据的**唯一副本**（库内那部分载荷在打包成功后已清空），所以缺包不能
    当作「这次没有数据」轻描淡写地跳过——这里逐个登记，由调用方决定是否报错。
    """
    copied: list[str] = []
    missing: list[str] = []
    total_bytes = 0
    if source_archive_dir is not None:
        for root in roots:
            state = root.get("payload_state")
            if state != "packed":
                continue
            run_id = root["run_id"]
            created_at = root.get("created_at_ms")
            day = _archive_day(created_at)
            source = source_archive_dir / day / f"{run_id}.json.gz"
            if not source.exists():
                missing.append(run_id)
                continue
            target = target_dir / day / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(run_id)
            total_bytes += target.stat().st_size
            _verify_gzip(target)
    return {
        "count": len(copied),
        "bytes": total_bytes,
        "missing": missing,
    }


def _archive_day(created_at_ms: int | None) -> str:
    """与写包侧同一个分日口径（东八区）。"""
    tz = DEFAULT_TZ
    if created_at_ms is None:
        return datetime.now(tz).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(created_at_ms / 1000, tz=tz).strftime("%Y-%m-%d")


def _verify_gzip(path: Path) -> None:
    """读一遍确认文件完整。复制中断会留下截断的 gzip，读的时候才炸更糟。"""
    with gzip.open(path, "rb") as handle:
        handle.read(1)


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def copy_baselines(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    phones: Sequence[str] | None,
) -> int:
    """连带增量基线表，返回行数。

    按手机号筛选（给了 ``--phone`` 就只带这些号码），**不按时间窗**：基线是「当前状态」，
    没有历史版本，一个号码一行的旧数据仍是本地跑 T0 的前提。源库没有这张表（旧库）时
    返回 0，不报错——那是「这份快照里没发生过增量」，不是数据损坏。
    """
    if not _has_table(source, BASELINE_TABLE):
        return 0
    if phones:
        marks = ",".join("?" for _ in phones)
        return copy_rows(source, target, BASELINE_TABLE, f"phone IN ({marks})", list(phones))
    return copy_rows(source, target, BASELINE_TABLE)


def export(
    source_path: Path,
    out_dir: Path,
    name: str,
    since_ms: int | None,
    until_ms: int | None,
    business_refs: Sequence[str] | None,
    dry_run: bool = False,
    source_archive_dir: Path | None = None,
) -> dict[str, Any]:
    """执行一次快照导出，返回 manifest。

    **库只是骨架，包才是运行时数据。** 打包之后库里的载荷列是空的，只拷库会得到一份
    处处空白的快照——而且看起来一切正常。因此凡是 ``payload_state='packed'`` 的根运行，
    它的包必须一起带出来，缺一个都要报出来。
    """
    if not source_path.exists():
        raise SystemExit(f"trace db not found: {source_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / f"{name}.db"
    if target_path.exists():
        raise SystemExit(f"refusing to overwrite: {target_path}")

    started = time.time()
    # ``mode=ro`` 只是表态；WAL 库在只读连线下也能读到已提交的最新内容。
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    try:
        source.execute("BEGIN")  # 一个读事务 = 一个一致快照
        run_ids = select_runs(source, since_ms, until_ms, business_refs)
        roots = select_root_runs(source, since_ms, until_ms, business_refs)
        if dry_run:
            packed = [r for r in roots if r.get("payload_state") == "packed"]
            purged = [r for r in roots if r.get("payload_state") == "purged"]
            baselines = 0
            if _has_table(source, BASELINE_TABLE):
                if business_refs:
                    marks = ",".join("?" for _ in business_refs)
                    baselines = source.execute(
                        f"SELECT COUNT(*) FROM {BASELINE_TABLE} "
                        f"WHERE phone IN ({marks})",
                        list(business_refs),
                    ).fetchone()[0]
                else:
                    baselines = source.execute(
                        f"SELECT COUNT(*) FROM {BASELINE_TABLE}"
                    ).fetchone()[0]
            return {
                "dry_run": True,
                "selected_runs": len(run_ids),
                "selected_roots": len(roots),
                "need_archive": len(packed),
                "purged": len(purged),
                "baselines": baselines,
            }

        target = sqlite3.connect(target_path)
        try:
            create_target(source, target)
            counts: dict[str, int] = {}
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                counts["workflow_runs"] = sum(
                    copy_rows(
                        source,
                        target,
                        "workflow_runs",
                        f"run_id IN ({','.join('?' for _ in chunk)})",
                        chunk,
                    )
                    for chunk in _chunks(run_ids)
                )
                for table in ("node_executions", "request_attempts"):
                    counts[table] = sum(
                        copy_rows(
                            source,
                            target,
                            table,
                            f"run_id IN ({','.join('?' for _ in chunk)})",
                            chunk,
                        )
                        for chunk in _chunks(run_ids)
                    )
                version_ids = [
                    row[0]
                    for row in source.execute(
                        f"SELECT DISTINCT definition_version_id FROM workflow_runs "
                        f"WHERE run_id IN ({placeholders}) "
                        f"AND definition_version_id IS NOT NULL",
                        run_ids,
                    )
                ]
                counts["definition_versions"] = sum(
                    copy_rows(
                        source,
                        target,
                        "definition_versions",
                        f"version_id IN ({','.join('?' for _ in chunk)})",
                        chunk,
                    )
                    for chunk in _chunks(version_ids)
                )
            else:
                counts = {table: 0 for table in TABLES}
            # 基线不挂在 run 上：即便本次一个运行都没选中，也要带（见 BASELINE_TABLE）。
            counts[BASELINE_TABLE] = copy_baselines(source, target, business_refs)
            target.commit()
            target.execute("VACUUM")
        finally:
            target.close()
        source.execute("COMMIT")
    finally:
        source.close()

    archive_target = out_dir / f"{name}.archives"
    archives = copy_archives(roots, source_archive_dir, archive_target)

    manifest = {
        "format": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "database": target_path.name,
        "bytes": target_path.stat().st_size,
        "source": {
            "path": str(source_path),
            "bytes": source_path.stat().st_size,
            "code_commit": _code_commit(),
        },
        "filters": {
            "since_ms": since_ms,
            "until_ms": until_ms,
            "business_refs": list(business_refs) if business_refs else None,
        },
        "counts": counts,
        "archive_dir": (name + ".archives") if archives["count"] else None,
        "archives": archives,
        "duration_ms": int((time.time() - started) * 1000),
    }
    manifest_path = out_dir / f"{name}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _code_commit() -> str | None:
    """取当前提交号。容器里没有 .git 或没有 git 时返回 None，不影响取数。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按时间窗/号码导出留痕库快照")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="源留痕库路径")
    parser.add_argument("--out-dir", required=True, help="快照输出目录")
    parser.add_argument("--name", default=None, help="快照文件名（不含扩展名）")
    parser.add_argument("--since", default=None, help="起始时间，含；YYYY-MM-DD 或 ISO 8601")
    parser.add_argument("--until", default=None, help="截止时间；只写日期时含当天整天")
    parser.add_argument(
        "--phone",
        default=None,
        help="业务标识（手机号）清单，逗号分隔；不给表示不按号码筛选",
    )
    parser.add_argument(
        "--archive-dir",
        default=os.environ.get("TRACE_ARCHIVE_DIR") or str(DEFAULT_ARCHIVE_DIR),
        help="运行时数据包目录；缺省取 TRACE_ARCHIVE_DIR，再缺省 <仓库>/data/trace-archives",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告会选中多少运行，不落盘"
    )
    args = parser.parse_args(argv)

    since_ms = parse_time(args.since)
    until_ms = parse_time(args.until, end_of_day=True)
    if since_ms is not None and until_ms is not None and since_ms >= until_ms:
        raise SystemExit("--since must be earlier than --until")

    phones = (
        [item.strip() for item in args.phone.split(",") if item.strip()]
        if args.phone
        else None
    )
    name = args.name or datetime.now().strftime("snapshot-%Y%m%d-%H%M%S")

    if args.dry_run:
        source = sqlite3.connect(f"file:{Path(args.db)}?mode=ro", uri=True)
        try:
            run_ids = select_runs(source, since_ms, until_ms, phones)
            roots = select_root_runs(source, since_ms, until_ms, phones)
            # 基线计数必须在关连接之前读：连接一关，dry-run 直接抛 ProgrammingError。
            if _has_table(source, BASELINE_TABLE):
                if phones:
                    marks = ",".join("?" for _ in phones)
                    baselines = source.execute(
                        f"SELECT COUNT(*) FROM {BASELINE_TABLE} WHERE phone IN ({marks})",
                        list(phones),
                    ).fetchone()[0]
                else:
                    baselines = source.execute(
                        f"SELECT COUNT(*) FROM {BASELINE_TABLE}"
                    ).fetchone()[0]
            else:
                baselines = 0
        finally:
            source.close()
        packed = sum(1 for r in roots if r.get("payload_state") == "packed")
        purged = sum(1 for r in roots if r.get("payload_state") == "purged")
        print(f"selected runs (roots + descendants): {len(run_ids)}")
        print(f"selected roots: {len(roots)}; need archive: {packed}; purged: {purged}")
        print(f"baselines to carry: {baselines}")
        return 0

    manifest = export(
        Path(args.db),
        Path(args.out_dir),
        name,
        since_ms,
        until_ms,
        phones,
        source_archive_dir=Path(args.archive_dir) if args.archive_dir else None,
    )
    counts = manifest["counts"]
    archives = manifest["archives"]
    print(
        f"exported {manifest['database']}: runs={counts['workflow_runs']} "
        f"nodes={counts['node_executions']} attempts={counts['request_attempts']} "
        f"definitions={counts['definition_versions']} "
        f"baselines={counts.get(BASELINE_TABLE, 0)} "
        f"size={manifest['bytes'] / 1024 / 1024:.1f}MB "
        f"archives={archives['count']} ({archives['bytes'] / 1024 / 1024:.1f}MB)"
    )
    if counts["workflow_runs"] == 0:
        print("WARNING: nothing selected; check the time window and phone filters", file=sys.stderr)
        return 2
    if archives["missing"]:
        print(
            f"ERROR: {len(archives['missing'])} 个已打包运行的包文件找不到，"
            "这些运行的运行时数据无法带出：",
            file=sys.stderr,
        )
        for run_id in archives["missing"]:
            print(f"  - {run_id}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

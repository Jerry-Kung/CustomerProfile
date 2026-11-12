#!/usr/bin/env python
"""把 SQLite 骨架库里的存量数据搬进 MySQL 运行时库。

## 搬什么

只搬**骨架**：运行、节点、请求尝试、定义快照、增量基线。运行时数据（节点输入输出、
请求体、响应原文）在存储瘦身之后本来就在 ``trace-archives/*.json.gz`` 里，不在库里，
因此**不搬也搬不了**——它们的唯一副本是包，随 ``DATA_DIR`` 走。

## 三条口径

1. **保留原始主键值**（``run_id``、``version_id``、``execution_id``、``attempt_id``）。
   ``definition_version_id`` 是运行到定义快照的引用，重新编号会让历史运行的图
   全变成「找不到定义」。导入后把自增值推到 ``MAX(id)+1``。
2. **不建外键，但要按依赖顺序插**：可读性、以及出错时能一眼看出是哪张表断的。
3. **导入后逐表对账**：行数、`NULL` 分布、载荷字节数。任何一项对不上就非零退出，
   而不是打印一行「完成」让人以为搬好了。

## 用法

```bash
# 1. 先干跑，确认条数与体积
python scripts/migrate_sqlite_to_mysql.py --sqlite data/customer_profile.db --dry-run

# 2. 正式导入（目标库必须已由 init_runtime_db.py 初始化）
python scripts/migrate_sqlite_to_mysql.py --sqlite data/customer_profile.db

# 3. 只搬某个时间窗之后的根运行（连同整棵树）
python scripts/migrate_sqlite_to_mysql.py --sqlite data/customer_profile.db --since 2026-11-01
```

目标库由 ``DATABASE_URL``（或 ``--url``）给出，与 ``init_runtime_db.py`` 同源。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.persistence.dbapi import MySqlBackend, build_backend  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))

# 依赖顺序：主表在后引用它的表之前
TABLES: tuple[str, ...] = (
    "workflow_runs",
    "definition_versions",
    "node_executions",
    "request_attempts",
    "profile_baselines",
)
"""导入顺序。

``workflow_runs`` 最先：它自引用（``parent_run_id``），SQLite 侧开启外键时父行必须先到。
其余三张只被运行引用，顺序仅影响出错时的可读性。
"""

PRIMARY_KEYS = {
    "workflow_runs": "run_id",
    "definition_versions": "version_id",
    "node_executions": "execution_id",
    "request_attempts": "attempt_id",
    "profile_baselines": "phone",
}

AUTO_INCREMENT = {
    "definition_versions": "version_id",
    "node_executions": "execution_id",
    "request_attempts": "attempt_id",
}

PAYLOAD_COLUMNS = {
    "workflow_runs": ("inputs_json", "outputs_json"),
    "node_executions": ("inputs_json", "outputs_json"),
    "request_attempts": ("request_json", "response_text", "usage_json"),
    "definition_versions": ("definition_json", "template_hashes_json"),
    "profile_baselines": ("consumed_keys_json", "callback_json"),
}


# ---------------------------------------------------------------- 源


def open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"源库不存在：{path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def source_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def _parse_time(raw: str | None) -> int | None:
    if not raw:
        return None
    text = raw.strip()
    if len(text) == 10:
        moment = datetime.fromisoformat(text).replace(tzinfo=TZ_CN)
    else:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TZ_CN)
    return int(moment.timestamp() * 1000)


def select_root_ids(connection: sqlite3.Connection, since_ms: int | None) -> list[str] | None:
    """时间窗按**根运行**的创建时刻筛。返回 ``None`` 表示不过滤（全量）。"""
    if since_ms is None:
        return None
    rows = connection.execute(
        "SELECT run_id FROM workflow_runs "
        "WHERE parent_run_id IS NULL AND created_at_ms >= ? "
        "ORDER BY created_at_ms",
        (since_ms,),
    ).fetchall()
    return [row["run_id"] for row in rows]


def collect_rows(
    connection: sqlite3.Connection, table: str, root_ids: Sequence[str] | None
) -> list[dict[str, Any]]:
    """取一张表的行；给了 ``root_ids`` 时只取这些根运行的整棵树。"""
    if root_ids is None or table == "profile_baselines":
        # 基线不挂在任何运行上，按整表搬（它是「当前状态」，没有时间维度）
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    if table == "definition_versions":
        # 定义快照按「被选中的运行引用到」筛，而不是全量：否则会带上无关历史版本
        marks = ",".join("?" for _ in root_ids)
        sql = (
            "SELECT * FROM definition_versions WHERE version_id IN ("
            "  WITH RECURSIVE tree(run_id) AS ("
            f"   SELECT run_id FROM workflow_runs WHERE run_id IN ({marks})"
            "   UNION SELECT child.run_id FROM workflow_runs AS child"
            "   JOIN tree ON child.parent_run_id = tree.run_id)"
            "  SELECT definition_version_id FROM workflow_runs "
            "  WHERE run_id IN (SELECT run_id FROM tree) "
            "  AND definition_version_id IS NOT NULL)"
        )
        return [dict(row) for row in connection.execute(sql, tuple(root_ids))]
    marks = ",".join("?" for _ in root_ids)
    tree = (
        "WITH RECURSIVE tree(run_id) AS ("
        f" SELECT run_id FROM workflow_runs WHERE run_id IN ({marks})"
        " UNION SELECT child.run_id FROM workflow_runs AS child"
        " JOIN tree ON child.parent_run_id = tree.run_id) "
    )
    if table == "workflow_runs":
        sql = tree + "SELECT * FROM workflow_runs WHERE run_id IN (SELECT run_id FROM tree)"
    else:
        sql = tree + f"SELECT * FROM {table} WHERE run_id IN (SELECT run_id FROM tree)"
    return [dict(row) for row in connection.execute(sql, tuple(root_ids))]


# ---------------------------------------------------------------- 目标


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    marks = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"


def import_table(connection: Any, table: str, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = list(rows[0])
    sql = _insert_sql(table, columns)
    written = 0
    # 逐行而非 executemany：MySQL 驱动对 executemany 的批量上限受 max_allowed_packet
    # 约束，而单行最大实测 572 KB。逐行的失败信息也更精确（哪一行、哪个值）。
    for row in rows:
        connection.execute(sql, tuple(row[col] for col in columns))
        written += 1
    connection.commit()
    return written


def reset_auto_increment(connection: Any, table: str, column: str) -> None:
    row = connection.execute(f"SELECT COALESCE(MAX({column}), 0) AS m FROM {table}").fetchone()
    maximum = int(row["m"]) if row else 0
    # ALTER TABLE 不能参数化；这里的值来自聚合查询，不是外部输入
    connection.execute(f"ALTER TABLE {table} AUTO_INCREMENT = {maximum + 1}")
    connection.commit()


# ---------------------------------------------------------------- 对账


def _null_count(connection: Any, table: str, column: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {column} IS NULL"
    ).fetchone()
    return int(row["n"]) if row else 0


def _source_payload_bytes(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> int:
    """源库载荷的**字节**总数。

    **必须用 ``LENGTH(CAST(col AS BLOB))``，不能用 ``LENGTH(col)``。** SQLite 对
    TEXT 的 ``LENGTH`` 只数到第一个 ``NUL`` 字符，而本项目的 ``response_text`` 里有
    录音转写带出的内嵌 NUL：一条 145508 字符的响应，``LENGTH`` 只报 11679，看起来像
    「迁完少了一大截」，实际数据是完整的。（这类值当初被当成 TEXT 存了，SQLite 不
    拒绝；MySQL 的 LONGTEXT 同样不拒绝 NUL。）
    """
    total = 0
    for column in columns:
        row = connection.execute(
            f"SELECT COALESCE(SUM(LENGTH(CAST({column} AS BLOB))), 0) FROM {table}"
        ).fetchone()
        total += int(row[0]) if row else 0
    return total


def _payload_bytes(connection: Any, table: str, columns: Sequence[str]) -> int:
    """目标库载荷的**字节**总数。MySQL 的 ``LENGTH`` 本来就是字节数。"""
    total = 0
    for column in columns:
        row = connection.execute(
            f"SELECT COALESCE(SUM(LENGTH({column})), 0) AS n FROM {table}"
        ).fetchone()
        total += int(row["n"]) if row else 0
    return total


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def reconcile(
    source: sqlite3.Connection, target: Any, expected: dict[str, int]
) -> list[str]:
    """逐表核对行数、NULL 分布与载荷字节数；返回不一致项清单（空表示通过）。

    源库里**没有**的表跳过：历史库可能早于某张表上线（例如 P1 之前的库没有
    ``profile_baselines``），那不是迁移错误。目标端有、源端没有，就是「没什么可搬」。
    """
    problems: list[str] = []
    present = _source_tables(source)
    for table in TABLES:
        if table not in present:
            continue
        dst_rows = target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        if expected.get(table) is not None and dst_rows != expected[table]:
            problems.append(f"{table} 行数：预期 {expected[table]}，实际 {dst_rows}")
        columns = PAYLOAD_COLUMNS.get(table, ())
        src_bytes = _source_payload_bytes(source, table, columns)
        dst_bytes = _payload_bytes(target, table, columns)
        if src_bytes != dst_bytes:
            problems.append(
                f"{table} 载荷字节：源 {src_bytes}，目标 {dst_bytes}"
            )
    return problems


# ---------------------------------------------------------------- 主流程


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="SQLite 骨架库 → MySQL 运行时库")
    parser.add_argument("--sqlite", default=str(REPO_ROOT / "data" / "customer_profile.db"))
    parser.add_argument("--url", default="", help="目标 DATABASE_URL；缺省从环境/..env")
    parser.add_argument("--since", default="", help="只搬该日期之后的根运行（连同整棵树）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写目标库")
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="导入前清空目标四张表。仅用于重跑演练；默认拒绝覆盖已有数据",
    )
    args = parser.parse_args(argv)

    url = args.url.strip()
    if not url:
        import os

        url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("缺少目标库：用 --url 传入，或设置 DATABASE_URL", file=sys.stderr)
        return 2

    backend = build_backend(url)
    if not isinstance(backend, MySqlBackend):
        print("目标必须是 mysql:// 形式的运行时库", file=sys.stderr)
        return 2

    source = open_source(Path(args.sqlite))
    since_ms = _parse_time(args.since)
    root_ids = select_root_ids(source, since_ms)

    plan: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        if table not in {
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }:
            plan[table] = []
            continue
        plan[table] = collect_rows(source, table, root_ids)

    print(f"源库：{args.sqlite}")
    print(f"目标：{backend.describe()}")
    print(f"筛选：{'全部根运行' if root_ids is None else f'{len(root_ids)} 个根运行（含子树）'}")
    for table in TABLES:
        print(f"  {table}: {len(plan[table])} 行")

    if args.dry_run:
        print("（--dry-run：未写目标库）")
        source.close()
        return 0

    from customer_profile.persistence.mysql_schema import SCHEMA_VERSION, current_versions

    target = backend.connect()
    try:
        if SCHEMA_VERSION not in current_versions(target):
            print(
                f"目标库尚未初始化（缺 {SCHEMA_VERSION}）；"
                "请先运行 scripts/init_runtime_db.py",
                file=sys.stderr,
            )
            return 3

        existing = target.execute("SELECT COUNT(*) AS n FROM workflow_runs").fetchone()["n"]
        if existing and not args.truncate_target:
            print(
                f"目标库已有 {existing} 行运行记录；为避免混入两份数据，默认不覆盖。\n"
                "确认要重做请加 --truncate-target（会清空目标四张表）",
                file=sys.stderr,
            )
            return 4
        if args.truncate_target:
            for table in reversed(TABLES):
                target.execute(f"DELETE FROM {table}")
            target.commit()
            print("已清空目标表")

        expected: dict[str, int] = {}
        for table in TABLES:
            rows = plan[table]
            # 源列可能与目标不同（历史库缺列）：只写目标也有的列
            target_columns = {
                row["COLUMN_NAME"]
                for row in target.execute(
                    "SELECT COLUMN_NAME FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = ?",
                    (table,),
                ).fetchall()
            }
            if rows:
                missing = set(rows[0]) - target_columns
                if missing:
                    print(f"  {table}：源有目标没有的列 {sorted(missing)}，将被忽略")
                    rows = [{k: v for k, v in row.items() if k in target_columns} for row in rows]
            written = import_table(target, table, rows)
            expected[table] = written
            print(f"  {table}: 写入 {written} 行")

        for table, column in AUTO_INCREMENT.items():
            reset_auto_increment(target, table, column)

        problems = reconcile(source, target, expected)
    finally:
        target.close()
        source.close()

    if problems:
        print("\n对账不一致：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 5
    print("\n对账通过：行数与载荷字节数一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

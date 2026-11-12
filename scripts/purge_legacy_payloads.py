#!/usr/bin/env python
"""存量就地清理：把旧运行的载荷列清空、打 ``purged``，**不写包**。

## 为什么不是迁移

2026-11-05 拍板：旧数据不搬。为历史行再建一条「打包 + 回灌」通道，要长期背着
「包写一半、库已清空」的风险，而旧行的问题基本都查过了。清理的收益（库从几个 GB
降到骨架大小）一分不少。

## 保留什么

- 骨架全部列：运行身份、状态、四类时间戳、耗时、错误、业务标识；
- 节点状态、``attempt_count``、每次外部调用的目标 URL 与状态码；
- ``definition_versions``（体积可忽略，决定历史运行的图与提示词还看得见）；
- **根运行的输出**（最终画像）。这是当时真正写出去的东西，profile 接口返回的是被人工
  确认覆盖后的当前值，替代不了它。

清理之后这些运行仍可列出、可看状态与错误，节点详情为空并标 ``purged``。

## 两种后端

- **SQLite**（本地、批次库）：``--db`` 指文件，备份用 ``VACUUM INTO``——一条语句拿到
  一致快照，不需要停写。
- **MySQL**（UAT / 生产）：``--source-url mysql://…``，备份用 ``mysqldump
  --single-transaction``。**不是** ``VACUUM INTO``：那是 SQLite 独占。

MySQL 侧的备份是外部进程，退出码与文件非空都要检查——``mysqldump`` 在权限不足时
会「建出一个 0 字节文件并返回非零」，只判文件存在就会拿着一份空备份去删数据。

用法：

    # SQLite
    python scripts/purge_legacy_payloads.py --db data/customer_profile.db --dry-run
    python scripts/purge_legacy_payloads.py --db data/customer_profile.db \\
        --backup data/customer_profile.db.bak-20261105

    # MySQL
    python scripts/purge_legacy_payloads.py --source-url mysql://… \\
        --backup /backup/runtime-before-purge.sql --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.persistence.store import PAYLOAD_PURGED  # noqa: E402

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted", "skipped")
"""只有终态运行才清。``queued`` / ``running`` 一动都不能动——它们还可能在写。"""

CHUNK = 200


def _is_mysql(source: Any) -> bool:
    return isinstance(source, str) and source.startswith("mysql://")


def _connect(source: Any):
    if _is_mysql(source):
        from customer_profile.persistence.dbapi import MySqlBackend, build_backend

        backend = build_backend(source)
        if not isinstance(backend, MySqlBackend):
            raise ValueError(f"不是 MySQL 连接串：{source}")
        return backend.connect()
    connection = sqlite3.connect(source, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _rows(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def _columns(connection: Any, table: str, mysql: bool) -> set[str]:
    if mysql:
        rows = _rows(
            connection,
            "SELECT COLUMN_NAME AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = ?",
            (table,),
        )
        return {row["name"] for row in rows}
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def select_targets(connection: Any, *, mysql: bool) -> list[str]:
    """待清理的根运行。

    **旧 schema 没有 ``payload_state`` 列时，所有终态行都是候选。** 这一列是存储瘦身
    才加的；在那之前的库，每一行都还没被清理过——按「列不存在 = 全部为 NULL」处理，
    而不是报 ``no such column`` 让人以为脚本坏了。清理时会先补列（见 ``main``）。
    """
    marks = ",".join("?" for _ in TERMINAL)
    has_state = "payload_state" in _columns(connection, "workflow_runs", mysql)
    state_clause = "AND payload_state IS NULL " if has_state else ""
    rows = _rows(
        connection,
        f"SELECT run_id FROM workflow_runs WHERE parent_run_id IS NULL "
        f"{state_clause}AND status IN ({marks}) ORDER BY created_at_ms",
        TERMINAL,
    )
    return [row["run_id"] for row in rows]


def ensure_state_column(connection: Any, *, mysql: bool) -> bool:
    """清理前补齐三列。返回是否补了。

    清理会写 ``payload_state`` / ``archived_at_ms``，旧库没有这两列。让脚本自己补，
    而不是要求先起一次服务（那会在生产上拉起一个不该在此时启动的进程）。
    """
    if "payload_state" in _columns(connection, "workflow_runs", mysql):
        return False
    if mysql:
        # 运行时 MySQL 由 mysql_schema 建表，一定带这两列；走到这里说明连错了库。
        raise SystemExit(
            "目标库的 workflow_runs 缺少 payload_state 列，与运行时 schema 不符；"
            "请确认 DATABASE_URL 指向的是运行时库"
        )
    for name, column_type in (
        ("payload_state", "TEXT"),
        ("payload_mode", "TEXT"),
        ("archived_at_ms", "INTEGER"),
    ):
        connection.execute(
            f"ALTER TABLE workflow_runs ADD COLUMN {name} {column_type}"
        )
    connection.commit()
    return True


def tree_run_ids(connection: Any, root_run_id: str) -> list[str]:
    rows = _rows(
        connection,
        "WITH RECURSIVE tree(run_id) AS ("
        " SELECT run_id FROM workflow_runs WHERE run_id = ?"
        " UNION ALL SELECT c.run_id FROM workflow_runs c"
        " JOIN tree ON c.parent_run_id = tree.run_id) "
        "SELECT run_id FROM tree",
        (root_run_id,),
    )
    return [row["run_id"] for row in rows]


def purge_one(connection: Any, root_run_id: str) -> int:
    """清一棵树。返回涉及的运行数。"""
    now = int(time.time() * 1000)
    run_ids = tree_run_ids(connection, root_run_id)
    for run_id in run_ids:
        connection.execute(
            "UPDATE node_executions SET inputs_json = NULL, outputs_json = NULL "
            "WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            "UPDATE request_attempts SET request_json = NULL, response_text = NULL, "
            "usage_json = NULL WHERE run_id = ?",
            (run_id,),
        )
        if run_id != root_run_id:
            connection.execute(
                "UPDATE workflow_runs SET outputs_json = NULL WHERE run_id = ?",
                (run_id,),
            )
        connection.execute(
            "UPDATE workflow_runs SET payload_state = ?, archived_at_ms = ? "
            "WHERE run_id = ? AND payload_state IS NULL",
            (PAYLOAD_PURGED, now, run_id),
        )
    return len(run_ids)


def backup_sqlite(connection: Any, backup: Path) -> None:
    """``VACUUM INTO``：一条语句拿到一致快照，不需要停写。"""
    connection.execute("VACUUM INTO ?", (str(backup),))


def backup_mysql(url: str, backup: Path) -> None:
    """``mysqldump --single-transaction``：InnoDB 的一致快照。

    **不经过 shell**：口令走 ``MYSQL_PWD`` 环境变量、参数用列表传，避免命令注入与
    在进程列表里暴露。``MYSQL_PWD`` 在部分环境里不推荐，但它比 ``-p<密码>``（会出现在
    ``ps`` 里）安全，且不需要交互式输入——这是无人值守脚本里最合适的取舍。
    """
    from urllib.parse import unquote

    if shutil.which("mysqldump") is None:
        raise SystemExit(
            "找不到 mysqldump；MySQL 备份需要客户端工具。\n"
            "可改用：mysqldump --single-transaction <库> > <文件> 手工备份后再执行本脚本"
        )

    rest = url.partition("://")[2]
    credentials, _, hostpart = rest.rpartition("@")
    user, _, password = credentials.partition(":")
    host, _, tail = hostpart.partition("/")
    database = tail.split("?", 1)[0]
    hostname, _, port = host.partition(":")

    command = [
        "mysqldump",
        "--single-transaction",
        "--no-tablespaces",
        "--host", hostname or "127.0.0.1",
        "--port", port or "3306",
        "--user", unquote(user),
        database,
    ]
    import os

    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = unquote(password)

    with backup.open("wb") as handle:
        result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, env=env)
    if result.returncode != 0:
        backup.unlink(missing_ok=True)
        raise SystemExit(
            "mysqldump 失败：" + result.stderr.decode("utf-8", errors="replace")[:500]
        )
    # 空文件意味着「命令成功但什么都没导出」，那是更危险的情形：看起来有备份。
    if backup.stat().st_size == 0:
        backup.unlink(missing_ok=True)
        raise SystemExit("mysqldump 产出了 0 字节文件，视为失败")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="存量留痕载荷就地清理（不迁移）")
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "customer_profile.db"),
                        help="源 SQLite 骨架库路径")
    parser.add_argument(
        "--source-url",
        default="",
        help="源 MySQL DSN（mysql://…）。给了它就不读 --db",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告会清理多少棵树")
    parser.add_argument(
        "--backup",
        default=None,
        help="正式执行前必须给出一份备份路径（SQLite 用 VACUUM INTO，MySQL 用 mysqldump）",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="SQLite 清完后回收文件空间（几 GB 的库会耗时数分钟；MySQL 忽略此项）",
    )
    args = parser.parse_args(argv)

    source: Any = args.source_url if args.source_url else Path(args.db)
    mysql = _is_mysql(source)
    if not mysql:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"ERROR: 库不存在 {db_path}", file=sys.stderr)
            return 2
        source = db_path

    connection = _connect(source)
    try:
        added = ensure_state_column(connection, mysql=mysql)
        if added:
            print("已补齐 workflow_runs 的 payload_state / payload_mode / archived_at_ms（旧 schema）")
        targets = select_targets(connection, mysql=mysql)
        if args.dry_run:
            nodes = 0
            for root in targets[:50]:
                rows = _rows(
                    connection,
                    "SELECT COUNT(*) AS n FROM node_executions WHERE run_id IN ("
                    "WITH RECURSIVE tree(run_id) AS (SELECT ? UNION ALL "
                    "SELECT c.run_id FROM workflow_runs c JOIN tree ON c.parent_run_id = tree.run_id)"
                    " SELECT run_id FROM tree)",
                    (root,),
                )
                nodes += int(rows[0]["n"]) if rows else 0
            print(f"待清理根运行：{len(targets)}（样本前 50 棵含节点 {nodes} 行）")
            print(f"后端：{'MySQL' if mysql else 'SQLite'}")
            return 0

        if not args.backup:
            print("ERROR: 正式执行必须提供 --backup（先备份，再删）", file=sys.stderr)
            return 2
        backup = Path(args.backup)
        if backup.exists():
            print(f"ERROR: 备份已存在，拒绝覆盖：{backup}", file=sys.stderr)
            return 2
        backup.parent.mkdir(parents=True, exist_ok=True)
        if mysql:
            backup_mysql(str(source), backup)
        else:
            backup_sqlite(connection, backup)
        print(f"已备份至 {backup}（{backup.stat().st_size / 1024 / 1024:.1f} MB）")

        done = 0
        for index, root_run_id in enumerate(targets, start=1):
            try:
                if not mysql:
                    connection.execute("BEGIN")
                purge_one(connection, root_run_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            done += 1
            if index % CHUNK == 0:
                print(f"  已清理 {index}/{len(targets)} 棵", flush=True)

        size = ""
        if not mysql and args.vacuum:
            # 回收文件空间。**不再无条件执行**：几 GB 的库 VACUUM 会持锁数分钟，
            # 而清理本身不需要它（删除的行进 freelist，后续写入会复用）。
            connection.execute("VACUUM")
            size = f"；库体积 {Path(source).stat().st_size / 1024 / 1024:.1f} MB"
        print(f"完成：清理 {done} 棵运行树{size}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

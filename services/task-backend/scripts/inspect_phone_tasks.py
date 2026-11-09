"""Read task diagnostics using the repository's configured MySQL connection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mysql.connector import Error as MySQLError

from src.config.settings import load_mysql_config
from src.infrastructure.db.client import MySQLClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", help="Exact customer phone number")
    parser.add_argument("--task-id", type=int, action="append", default=[], help="Task ID; repeat for several IDs")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows, from 1 to 1000")
    return parser


def build_query(phone: str | None, task_ids: list[int], limit: int) -> tuple[str, tuple]:
    if not (phone and phone.strip()) and not task_ids:
        raise ValueError("at least one phone number or task ID is required")
    if not 1 <= limit <= 1000 or any(task_id <= 0 for task_id in task_ids):
        raise ValueError("limit must be 1..1000 and task IDs must be positive")
    filters = []
    params: list[object] = []
    if phone and phone.strip():
        filters.append("customer_phonenumber = %s")
        params.append(phone)
    if task_ids:
        filters.append("id IN (" + ", ".join("%s" for _ in task_ids) + ")")
        params.extend(task_ids)
    sql = (
        "SELECT id, customer_phonenumber, task_type, work_env, batch_id, status, "
        "workflow_target_id, workflow_target_revision, workflow_run_id, attempt_count, "
        "last_error_code, last_error_stage, error_message, lease_expires_at, updated_at "
        "FROM customer_profile_tasks WHERE (" + " OR ".join(filters) + ") "
        "ORDER BY updated_at DESC, id DESC LIMIT %s"
    )
    return sql, (*params, limit)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sql, params = build_query(args.phone, args.task_id, args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    connection = None
    cursor = None
    try:
        connection = MySQLClient(load_mysql_config()).connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params)
        print(json.dumps(cursor.fetchall(), ensure_ascii=False, indent=2, default=str))
        return 0
    except (ValueError, MySQLError) as exc:
        print(f"inspection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Quick inspection script for the duplicate-execution investigation.

Fill in the connection fields below, then run:
    python scripts/inspect_phone_tasks.py

Outputs:
  - SQL 1: rows for id IN (928, 942) OR customer_phonenumber = TARGET_PHONE
  - SQL 2: all rows for TARGET_PHONE
"""

from __future__ import annotations

import pymysql


HOST = "10.81.67.210"
PORT = 33306
USER = "mengshi"
PASSWORD = "MengShi#$12"
DATABASE = "customer_profile"

TARGET_IDS = (928, 942)
TARGET_PHONE = "13332887454"


SQL_1 = """
SELECT id, customer_phonenumber, task_type, status, error_message,
       created_at, updated_at
FROM customer_profile_tasks
WHERE id IN (%s, %s)
   OR customer_phonenumber = %s
ORDER BY id
"""

SQL_2 = """
SELECT id, customer_phonenumber, status,
       LEFT(error_message, 80) AS err,
       updated_at
FROM customer_profile_tasks
WHERE customer_phonenumber = %s
"""


def _print_rows(title: str, columns: list[str], rows: list[tuple]) -> None:
    print(f"\n===== {title} =====")
    if not rows:
        print("(no rows)")
        return
    widths = [len(c) for c in columns]
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "-+-".join("-" * w for w in widths)
    print(header)
    print(sep)
    for row in str_rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> int:
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        connect_timeout=5,
    )
    try:
        cursor = conn.cursor()

        cursor.execute(SQL_1, (TARGET_IDS[0], TARGET_IDS[1], TARGET_PHONE))
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        _print_rows(
            f"SQL 1: id IN {TARGET_IDS} OR customer_phonenumber = {TARGET_PHONE!r}",
            columns,
            rows,
        )

        cursor.execute(SQL_2, (TARGET_PHONE,))
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        _print_rows(
            f"SQL 2: all rows for customer_phonenumber = {TARGET_PHONE!r}",
            columns,
            rows,
        )

        cursor.close()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

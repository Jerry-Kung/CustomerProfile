"""Apply db/migrations/003_add_task_type.sql against the configured MySQL.

Usage (from project root, with the standard .env present):

    python scripts/apply_migration_003.py

The script reads MYSQL_* from the project's .env (auto-loaded via
python-dotenv, same path as src/config/settings.py). It is idempotent —
the SQL file itself inspects information_schema before each DDL, so this
script is safe to run multiple times. Designed to be run once during the
multi-task-type rollout (see docs/deploy-and-run.md §5).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "db" / "migrations" / "003_add_task_type.sql"
TABLE_NAME = "customer_profile_tasks"
COLUMN_NAME = "task_type"
LEGACY_UNIQUE = "uk_customer_profile_tasks_phone"
NEW_UNIQUE = "uk_customer_profile_tasks_phone_type"


def _build_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    return logging.getLogger("apply_migration_003")


def _load_env(logger: logging.Logger) -> dict[str, str]:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("loaded .env path=%s", env_path)
    else:
        logger.warning(".env not found at %s; falling back to OS environment", env_path)

    required = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"missing required environment variables: {', '.join(missing)} "
            "(fill them into .env or export them before re-running)"
        )

    try:
        port = int(os.getenv("MYSQL_PORT", "3306"))
    except ValueError as exc:
        raise RuntimeError(f"invalid integer value for MYSQL_PORT: {exc}") from exc

    return {
        "host": os.environ["MYSQL_HOST"],
        "port": port,
        "user": os.environ["MYSQL_USER"],
        "password": os.environ["MYSQL_PASSWORD"],
        "database": os.environ["MYSQL_DATABASE"],
    }


def _split_statements(sql: str) -> list[str]:
    """Strip comments + split on ';' the same way migration_runner.py does."""
    cleaned_lines = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _check_column(cursor, database: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (database, TABLE_NAME, COLUMN_NAME),
    )
    return cursor.fetchone() is not None


def _check_index(cursor, database: str, index_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.statistics
        WHERE table_schema = %s AND table_name = %s AND index_name = %s
        LIMIT 1
        """,
        (database, TABLE_NAME, index_name),
    )
    return cursor.fetchone() is not None


def _log_snapshot(logger: logging.Logger, cursor, database: str, when: str) -> None:
    has_column = _check_column(cursor, database)
    has_legacy = _check_index(cursor, database, LEGACY_UNIQUE)
    has_new = _check_index(cursor, database, NEW_UNIQUE)
    logger.info(
        "schema_%s table=%s task_type_column=%s legacy_unique(%s)=%s new_unique(%s)=%s",
        when,
        TABLE_NAME,
        has_column,
        LEGACY_UNIQUE,
        has_legacy,
        NEW_UNIQUE,
        has_new,
    )


def main() -> int:
    logger = _build_logger()

    if not MIGRATION_PATH.exists():
        logger.error("migration file not found: %s", MIGRATION_PATH)
        return 1

    mysql_kwargs = _load_env(logger)
    logger.info(
        "connecting host=%s port=%s user=%s database=%s",
        mysql_kwargs["host"],
        mysql_kwargs["port"],
        mysql_kwargs["user"],
        mysql_kwargs["database"],
    )

    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    statements = _split_statements(migration_sql)
    logger.info("loaded migration file=%s statements=%d", MIGRATION_PATH.name, len(statements))

    connection = mysql.connector.connect(connection_timeout=10, **mysql_kwargs)
    try:
        cursor = connection.cursor()
        try:
            _log_snapshot(logger, cursor, mysql_kwargs["database"], when="before")

            for idx, statement in enumerate(statements, start=1):
                preview = " ".join(statement.split())[:120]
                logger.info("executing stmt %d/%d :: %s", idx, len(statements), preview)
                cursor.execute(statement)
                # Drain any result set so subsequent statements work cleanly.
                try:
                    cursor.fetchall()
                except mysql.connector.errors.InterfaceError:
                    pass

            connection.commit()
            logger.info("commit ok")

            _log_snapshot(logger, cursor, mysql_kwargs["database"], when="after")

            ok = (
                _check_column(cursor, mysql_kwargs["database"])
                and _check_index(cursor, mysql_kwargs["database"], NEW_UNIQUE)
                and not _check_index(cursor, mysql_kwargs["database"], LEGACY_UNIQUE)
            )
            if ok:
                logger.info("migration_succeeded file=%s", MIGRATION_PATH.name)
                return 0
            logger.error(
                "migration_verification_failed: expected task_type column + new unique key "
                "present and legacy unique key absent — please inspect the DB manually"
            )
            return 2
        finally:
            cursor.close()
    except mysql.connector.Error as exc:
        connection.rollback()
        logger.error("migration_failed mysql_error=%s", exc)
        return 1
    finally:
        connection.close()
        logger.info("connection closed")


if __name__ == "__main__":
    sys.exit(main())

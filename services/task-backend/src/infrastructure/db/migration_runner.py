from __future__ import annotations

import time
from pathlib import Path

import mysql.connector
from mysql.connector import Error as MySQLError

from src.config.logging_utils import get_logger
from src.config.observability import elapsed_ms, safe_text
from src.config.settings import MigrationConfig, MySQLConfig
from src.contracts.registry import ContractPaths


def run_migration(
    config: MySQLConfig,
    paths: ContractPaths,
    wait: MigrationConfig | None = None,
) -> None:
    """Apply every db/migrations/*.sql file in filename order.

    All migrations in this repo are idempotent (CREATE TABLE IF NOT EXISTS or
    information_schema checks), so re-running them on every container start is
    safe.

    `wait` enables a connect-retry loop for startup races where MySQL is not yet
    ready. When None, connect once and fail fast.
    """
    logger = get_logger("migration")
    files = _discover_migration_files(paths.migrations_dir)
    if not files:
        logger.warning("no_migrations_found dir=%s", paths.migrations_dir)
        return

    connection = _connect_with_retry(config, wait, logger)
    try:
        for sql_path in files:
            _apply_one(connection, sql_path, logger)
    finally:
        connection.close()


def _discover_migration_files(migrations_dir: Path) -> list[Path]:
    return sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name)


def _connect_with_retry(
    config: MySQLConfig,
    wait: MigrationConfig | None,
    logger,
):
    max_tries = wait.db_wait_max_tries if wait else 1
    interval = wait.db_wait_interval_seconds if wait else 0.0
    last_error: Exception | None = None
    start = time.perf_counter()
    for attempt in range(1, max_tries + 1):
        try:
            connection = mysql.connector.connect(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password,
                database=config.database,
                connection_timeout=10,
            )
            logger.info(
                "migration_mysql_connected host=%s port=%s database=%s attempt=%s elapsed_ms=%s",
                config.host,
                config.port,
                config.database,
                attempt,
                elapsed_ms(start),
            )
            return connection
        except MySQLError as exc:
            last_error = exc
            if attempt >= max_tries:
                break
            logger.info(
                "mysql_not_ready attempt=%d/%d wait_seconds=%.1f error=%s",
                attempt,
                max_tries,
                interval,
                safe_text(exc, 300),
            )
            time.sleep(interval)
    logger.error(
        "migration_mysql_unreachable host=%s port=%s database=%s attempts=%s elapsed_ms=%s error_type=%s detail=%s",
        config.host,
        config.port,
        config.database,
        max_tries,
        elapsed_ms(start),
        type(last_error).__name__ if last_error else None,
        safe_text(last_error, 500),
    )
    raise RuntimeError(
        f"mysql unreachable after {max_tries} attempts: {last_error}"
    ) from last_error


def _apply_one(connection, sql_path: Path, logger) -> None:
    start = time.perf_counter()
    sql = sql_path.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    logger.info(
        "migration_start file=%s statement_count=%s size_bytes=%s",
        sql_path.name,
        len(statements),
        len(sql.encode("utf-8")),
    )
    cursor = connection.cursor()
    try:
        # mysql-connector-python 9.x removed cursor.execute(..., multi=True).
        # Split the file into individual statements and execute them on the
        # same connection — user variables (@add_task_type, etc.) live at the
        # session level, so the SET / PREPARE / EXECUTE pattern in 002 and 003
        # keeps its context across the separate execute() calls.
        for index, statement in enumerate(statements, start=1):
            statement_start = time.perf_counter()
            try:
                cursor.execute(statement)
                if cursor.with_rows:
                    cursor.fetchall()
            except Exception:
                connection.rollback()
                logger.exception(
                    "migration_statement_failed file=%s statement_index=%s statement_count=%s statement_verb=%s elapsed_ms=%s",
                    sql_path.name,
                    index,
                    len(statements),
                    statement.split(None, 1)[0].upper() if statement.split() else "UNKNOWN",
                    elapsed_ms(statement_start),
                )
                raise
            logger.debug(
                "migration_statement_completed file=%s statement_index=%s statement_count=%s elapsed_ms=%s",
                sql_path.name,
                index,
                len(statements),
                elapsed_ms(statement_start),
            )
    finally:
        cursor.close()
    connection.commit()
    logger.info(
        "migration_done file=%s statement_count=%s elapsed_ms=%s",
        sql_path.name,
        len(statements),
        elapsed_ms(start),
    )


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into statements on `;`, respecting `--` line comments
    and single-quoted string literals (with `''` as the escaped-quote form).

    Block comments (`/* */`), backtick identifiers, and DELIMITER directives are
    not used in this project's migrations, so they are intentionally not handled.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_string = False
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if in_string:
            buffer.append(ch)
            if ch == "'":
                if i + 1 < length and sql[i + 1] == "'":
                    buffer.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "-" and i + 1 < length and sql[i + 1] == "-":
            newline = sql.find("\n", i)
            if newline == -1:
                break
            i = newline + 1
            buffer.append("\n")
            continue
        if ch == "'":
            in_string = True
            buffer.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            i += 1
            continue
        buffer.append(ch)
        i += 1
    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements

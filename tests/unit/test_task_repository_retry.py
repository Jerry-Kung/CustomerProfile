from __future__ import annotations

import logging

from src.infrastructure.db.task_repository import MySQLTaskRepository


class _RecoveryCursor:
    def __init__(self, task_update_rowcounts: dict[str, int]):
        self._task_update_rowcounts = task_update_rowcounts
        self.rowcount = 0
        self.executed: list[str] = []

    def execute(self, sql: str, _params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if "workflow_run_id IS NOT NULL" in normalized:
            self.rowcount = self._task_update_rowcounts["known_run"]
        elif "attempt_count = 0" in normalized:
            self.rowcount = self._task_update_rowcounts["before_submit"]
        elif "attempt_count > 0" in normalized:
            self.rowcount = self._task_update_rowcounts["unknown_submit"]
        else:
            self.rowcount = 0

    def close(self) -> None:
        pass


class _RecoveryConnection:
    def __init__(self, task_update_rowcounts: dict[str, int]):
        self.cursor_instance = _RecoveryCursor(task_update_rowcounts)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _RecoveryDB:
    def __init__(self, connection: _RecoveryConnection):
        self.connection = connection

    def connect(self) -> _RecoveryConnection:
        return self.connection


class _StallCursor:
    def __init__(self, *, main_update_rowcount: int):
        self._main_update_rowcount = main_update_rowcount
        self.rowcount = 0
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "workflow_run_started_at <= DATE_SUB(CURRENT_TIMESTAMP" in normalized:
            self.rowcount = self._main_update_rowcount
        elif "UPDATE customer_profile_task_attempts" in normalized:
            self.rowcount = 1
        else:
            self.rowcount = 0


class _StallConnection:
    def __init__(self, *, main_update_rowcount: int):
        self.cursor_instance = _StallCursor(main_update_rowcount=main_update_rowcount)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _StallDB:
    def __init__(self, connection: _StallConnection):
        self.connection = connection

    def connect(self) -> _StallConnection:
        return self.connection


class _ScheduleCursor:
    def __init__(self):
        self.rowcount = 0
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        self.rowcount = 1 if "UPDATE customer_profile_tasks" in normalized else 1


class _ScheduleConnection:
    def __init__(self):
        self.cursor_instance = _ScheduleCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _ScheduleDB:
    def __init__(self, connection: _ScheduleConnection):
        self.connection = connection

    def connect(self) -> _ScheduleConnection:
        return self.connection


def _repository(task_update_rowcounts: dict[str, int]) -> tuple[MySQLTaskRepository, _RecoveryConnection]:
    connection = _RecoveryConnection(task_update_rowcounts)
    return (
        MySQLTaskRepository(
            _RecoveryDB(connection),
            logger=logging.getLogger("test_task_repository_retry"),
        ),
        connection,
    )


def test_expired_lease_before_attempt_reservation_is_safely_requeued():
    repo, connection = _repository(
        {"known_run": 0, "before_submit": 1, "unknown_submit": 0}
    )

    assert repo.recover_expired_retry_leases(limit=10) == (1, 0)

    sql = "\n".join(connection.cursor_instance.executed)
    assert "workflow_run_id IS NOT NULL" in sql
    assert "attempt_count = 0" in sql
    assert "attempt_count > 0" in sql
    assert connection.commits == 1
    assert connection.closed is True


def test_expired_lease_after_attempt_reservation_is_not_blindly_resubmitted():
    repo, connection = _repository(
        {"known_run": 0, "before_submit": 0, "unknown_submit": 1}
    )

    assert repo.recover_expired_retry_leases(limit=10) == (0, 1)

    sql = "\n".join(connection.cursor_instance.executed)
    assert "attempt_count > 0" in sql
    assert "QINGTIAN_SUBMIT_UNKNOWN" in sql
    assert "UPDATE customer_profile_task_attempts AS attempt" in sql
    assert connection.commits == 1
    assert connection.closed is True


def test_schedule_retry_can_atomically_switch_the_execution_backend():
    connection = _ScheduleConnection()
    repo = MySQLTaskRepository(
        _ScheduleDB(connection),
        logger=logging.getLogger("test_task_repository_retry"),
    )

    assert repo.schedule_retry(
        task_id=91,
        generation=7,
        claim_token="claim-91",
        error_code="QINGTIAN_WORKFLOW_FAILED",
        error_stage="workflow",
        error_message="local attempt failed",
        delay_seconds=120,
        retain_workflow_run=False,
        terminal_attempt=True,
        attempt_no=2,
        next_execution_backend="commercial",
    )

    task_update = next(
        item
        for item in connection.cursor_instance.executed
        if "UPDATE customer_profile_tasks" in item[0]
    )
    assert "execution_backend = COALESCE(%s, execution_backend)" in task_update[0]
    assert task_update[1][0] == "commercial"
    assert connection.commits == 1
    assert connection.closed is True


def test_stalled_run_lifetime_is_checked_and_finalized_with_database_time():
    connection = _StallConnection(main_update_rowcount=1)
    repo = MySQLTaskRepository(
        _StallDB(connection),
        logger=logging.getLogger("test_task_repository_retry"),
    )

    assert repo.mark_stalled_if_run_lifetime_exceeded(
        task_id=91,
        generation=7,
        claim_token="claim-91",
        attempt_no=1,
        max_run_lifetime_seconds=7200,
    )

    statements = connection.cursor_instance.executed
    main_sql, main_params = next(
        (sql, params)
        for sql, params in statements
        if "workflow_run_started_at <= DATE_SUB(CURRENT_TIMESTAMP" in sql
    )
    assert "workflow_run_id IS NOT NULL" in main_sql
    assert "QINGTIAN_RUN_STALLED" not in main_sql  # parameterized rather than interpolated
    assert main_params is not None
    assert main_params[-1] == 7200
    audit_sql = next(sql for sql, _ in statements if "UPDATE customer_profile_task_attempts" in sql)
    assert "retryable = 0" in audit_sql
    assert connection.commits == 1
    assert connection.closed is True

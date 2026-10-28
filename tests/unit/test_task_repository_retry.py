from __future__ import annotations

import logging

from src.infrastructure.db.task_repository import (
    MySQLTaskRepository,
    _select_next_eligible_sql,
)


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
        elif "AND NOT EXISTS" in normalized:
            self.rowcount = self._task_update_rowcounts["before_submit"]
        elif "AND EXISTS" in normalized:
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


class _LeaseCursor:
    def __init__(self, *, update_rowcount: int, ownership_matches: bool):
        self._update_rowcount = update_rowcount
        self._ownership_matches = ownership_matches
        self.rowcount = 0
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("UPDATE customer_profile_tasks"):
            self.rowcount = self._update_rowcount
        else:
            self.rowcount = 0

    def fetchone(self):
        return (1,) if self._ownership_matches else None


class _LeaseConnection:
    def __init__(self, *, update_rowcount: int, ownership_matches: bool):
        self.cursor_instance = _LeaseCursor(
            update_rowcount=update_rowcount,
            ownership_matches=ownership_matches,
        )
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


class _LeaseDB:
    def __init__(self, connection: _LeaseConnection):
        self.connection = connection

    def connect(self) -> _LeaseConnection:
        return self.connection


class _LegacyRequeueCursor:
    def __init__(self, *, rowcount: int):
        self._configured_rowcount = rowcount
        self.rowcount = 0
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        self.rowcount = self._configured_rowcount


class _LegacyRequeueConnection:
    def __init__(self, *, rowcount: int):
        self.cursor_instance = _LegacyRequeueCursor(rowcount=rowcount)
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


class _LegacyRequeueDB:
    def __init__(self, connection: _LegacyRequeueConnection):
        self.connection = connection

    def connect(self) -> _LegacyRequeueConnection:
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
    assert "current_attempt.status = 'submitting'" in sql
    assert "AND NOT EXISTS" in sql
    assert "AND EXISTS" in sql
    assert connection.commits == 1
    assert connection.closed is True


def test_expired_lease_after_attempt_reservation_is_not_blindly_resubmitted():
    repo, connection = _repository(
        {"known_run": 0, "before_submit": 0, "unknown_submit": 1}
    )

    assert repo.recover_expired_retry_leases(limit=10) == (0, 1)

    sql = "\n".join(connection.cursor_instance.executed)
    assert "AND EXISTS" in sql
    assert "QINGTIAN_SUBMIT_UNKNOWN" in sql
    assert "UPDATE customer_profile_task_attempts AS attempt" in sql
    assert connection.commits == 1
    assert connection.closed is True


def test_same_second_lease_refresh_accepts_unchanged_but_still_owned_row():
    connection = _LeaseConnection(update_rowcount=0, ownership_matches=True)
    repo = MySQLTaskRepository(
        _LeaseDB(connection),
        logger=logging.getLogger("test_task_repository_retry"),
    )

    assert repo.refresh_lease(
        task_id=91,
        generation=7,
        claim_token="claim-91",
        lease_seconds=180,
    )

    statements = connection.cursor_instance.executed
    assert len(statements) == 2
    assert statements[1][0].startswith("SELECT 1 FROM customer_profile_tasks")
    assert "generation = %s" in statements[1][0]
    assert "claim_token = %s" in statements[1][0]
    assert statements[1][1] == (91, 7, "claim-91")
    assert connection.commits == 1
    assert connection.closed is True


def test_lease_refresh_rejects_row_when_fencing_token_no_longer_matches():
    connection = _LeaseConnection(update_rowcount=0, ownership_matches=False)
    repo = MySQLTaskRepository(
        _LeaseDB(connection),
        logger=logging.getLogger("test_task_repository_retry"),
    )

    assert not repo.refresh_lease(
        task_id=91,
        generation=7,
        claim_token="stale-claim",
        lease_seconds=180,
    )
    assert connection.commits == 1
    assert connection.closed is True


def test_stale_unleased_legacy_task_is_requeued_as_a_new_unbound_generation():
    connection = _LegacyRequeueConnection(rowcount=10)
    repo = MySQLTaskRepository(
        _LegacyRequeueDB(connection),
        default_retry_policy_version="throughput_first_v1",
        logger=logging.getLogger("test_task_repository_retry"),
    )

    assert repo.requeue_stale_processing(timeout_seconds=2430, limit=100) == 10

    sql, params = connection.cursor_instance.executed[0]
    assert "generation = generation + 1" in sql
    assert "workflow_target_id = NULL" in sql
    assert "workflow_target_revision = NULL" in sql
    assert "workflow_run_id = NULL" in sql
    assert "attempt_count = 0" in sql
    assert "lease_expires_at IS NULL" in sql
    assert "claim_token IS NULL" in sql
    assert "workflow_run_id IS NULL" in sql
    assert "dify_run_id IS NULL" in sql
    assert "attempt_count = 0" in sql
    assert params == ("throughput_first_v1", 2430, 100)
    assert connection.commits == 1
    assert connection.closed is True


def test_schedule_retry_can_atomically_switch_the_execution_pool_and_release_target():
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
        next_execution_pool="commercial",
    )

    task_update = next(
        item
        for item in connection.cursor_instance.executed
        if "UPDATE customer_profile_tasks" in item[0]
    )
    assert "execution_pool = COALESCE(%s, execution_pool)" in task_update[0]
    assert "workflow_target_id = CASE WHEN retry_policy_version = 'single_target_v1'" in task_update[0]
    assert task_update[1][0] == "commercial"
    assert task_update[1][2] == "commercial"
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


def test_target_aware_claim_prefers_fresh_throughput_target_then_allows_reuse():
    sql, params = _select_next_eligible_sql(
        True,
        True,
        True,
        workflow_target_id="commercial-02",
        workflow_target_revision="v1",
        execution_pool="commercial",
        allow_new_tasks=True,
        allow_unbound_retries=True,
        task_types=("customer_profile",),
        work_envs=("production",),
    )
    normalized = " ".join(sql.split())

    assert "task.workflow_target_id = %s" in normalized
    assert "task.workflow_target_revision = %s" in normalized
    assert "task.execution_pool = %s OR task.execution_pool = 'any'" in normalized
    assert "prior_target.workflow_target_id = %s" in normalized
    assert "task.next_retry_at <= DATE_SUB( CURRENT_TIMESTAMP, INTERVAL %s SECOND )" in normalized
    assert "task.task_type IN (%s)" in normalized
    assert "task.work_env IN (%s)" in normalized
    assert params == (
        "commercial-02",
        "v1",
        "commercial",
        1,
        "commercial",
        "commercial-02",
        5,
        1,
        "customer_profile",
        "production",
    )

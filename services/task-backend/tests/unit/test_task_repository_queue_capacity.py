from __future__ import annotations

import pytest

from src.domain.task.errors import TaskQueueFullError
from src.infrastructure.db.task_repository import MySQLTaskRepository


class _CountCursor:
    def __init__(self, queued_counts: dict[str, int]):
        self._queued_counts = queued_counts
        self._result: dict[str, int] = {"total": 0}
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "task_type = %s" in normalized:
            self._result = {"total": self._queued_counts[str(params[0])]}
        else:
            self._result = {"total": sum(self._queued_counts.values())}

    def fetchone(self) -> dict[str, int]:
        return self._result


class _CountConnection:
    def __init__(self, queued_counts: dict[str, int]):
        self.cursor_instance = _CountCursor(queued_counts)

    def cursor(self, *, dictionary: bool = False) -> _CountCursor:
        assert dictionary is True
        return self.cursor_instance


class _AdmissionCursor:
    def __init__(self):
        self._result = None

    def execute(self, sql: str, _params=None) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT GET_LOCK"):
            self._result = {"acquired": 1}
        elif normalized.startswith("SELECT status"):
            self._result = None

    def fetchone(self):
        return self._result


class _AdmissionConnection:
    def __init__(self):
        self.cursor_instance = _AdmissionCursor()
        self.rollbacks = 0
        self.closed = False

    def cursor(self, *, dictionary: bool = False) -> _AdmissionCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _AdmissionDB:
    def __init__(self, connection: _AdmissionConnection):
        self.connection = connection

    def connect(self) -> _AdmissionConnection:
        return self.connection


def test_count_queued_isolated_by_task_type():
    connection = _CountConnection(
        {"customer_profile": 0, "invitation_strategy": 1000}
    )
    repository = MySQLTaskRepository(db_client=object())
    repository._has_task_type_column_cache = True

    assert (
        repository.count_queued(task_type="customer_profile", conn=connection) == 0
    )
    assert (
        repository.count_queued(task_type="invitation_strategy", conn=connection)
        == 1000
    )
    assert all(
        "WHERE status = 'queued' AND task_type = %s" in sql
        for sql, _params in connection.cursor_instance.executed
    )
    assert [
        params for _sql, params in connection.cursor_instance.executed
    ] == [("customer_profile",), ("invitation_strategy",)]


def test_count_queued_without_task_type_remains_global():
    connection = _CountConnection(
        {"customer_profile": 7, "invitation_strategy": 11}
    )
    repository = MySQLTaskRepository(db_client=object())

    assert repository.count_queued(conn=connection) == 18
    sql, params = connection.cursor_instance.executed[0]
    assert "task_type = %s" not in sql
    assert params is None


def test_admission_checks_capacity_for_submitted_task_type(monkeypatch):
    connection = _AdmissionConnection()
    repository = MySQLTaskRepository(
        _AdmissionDB(connection),
        queue_max_waiting=1000,
    )
    repository._has_work_env_column_cache = True
    repository._has_batch_id_column_cache = True
    repository._has_task_type_column_cache = True
    counted_task_types: list[str] = []

    def fake_count_queued(*, task_type: str, conn) -> int:
        assert conn is connection
        counted_task_types.append(task_type)
        return 1000

    monkeypatch.setattr(repository, "count_queued", fake_count_queued)

    with pytest.raises(TaskQueueFullError) as exc_info:
        repository.upsert_queued(
            "13800138000",
            task_type="customer_profile",
        )

    assert counted_task_types == ["customer_profile"]
    assert exc_info.value.max_waiting == 1000
    assert exc_info.value.queued_count == 1000
    assert connection.rollbacks == 1
    assert connection.closed is True

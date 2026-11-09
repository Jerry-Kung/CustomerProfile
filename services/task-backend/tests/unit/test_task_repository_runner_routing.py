from __future__ import annotations

from dataclasses import asdict, fields
import re
import sqlite3

import pytest

from src.domain.task.models import TaskRecord
from src.infrastructure.db.task_repository import MySQLTaskRepository, _select_next_queued_sql, _select_next_eligible_sql


class _ClaimCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.rowcount = 0

    def execute(self, sql: str, params=None):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if normalized == "START TRANSACTION":
            self.connection.database.execute("BEGIN")
            return
        # SQLite executes the actual generated filters; only MySQL locking
        # syntax and placeholders differ in this focused routing test.
        sqlite_sql = sql.replace("FOR UPDATE SKIP LOCKED", "").replace("%s", "?")
        self.result = self.connection.database.execute(sqlite_sql, params or ())
        self.rowcount = self.result.rowcount

    def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row else None


class _ClaimConnection:
    def __init__(self, rows):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        definitions = [
            field.name + (" INTEGER PRIMARY KEY" if field.name == "id" else " TEXT")
            for field in fields(TaskRecord)
        ]
        self.database.execute("CREATE TABLE customer_profile_tasks (" + ",".join(definitions) + ")")
        for row in rows:
            values = asdict(row)
            self.database.execute(
                "INSERT INTO customer_profile_tasks (" + ",".join(values) + ") VALUES ("
                + ",".join("?" for _ in values) + ")",
                tuple(values.values()),
            )
        self.database.commit()
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, dictionary=False):
        assert dictionary
        return _ClaimCursor(self)

    def commit(self):
        self.database.commit()
        self.commits += 1

    def rollback(self):
        self.database.rollback()
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _ClaimDB:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def task(task_id, *, task_type="customer_profile", work_env="production", **overrides):
    return TaskRecord(
        id=task_id,
        customer_phonenumber="13800138000",
        task_type=task_type,
        work_env=work_env,
        status="queued",
        updated_at="2026-10-04T10:00:00",
        **overrides,
    )


@pytest.mark.parametrize("has_type, has_env, types, envs", [
    (False, True, ("customer_profile",), ("production",)),
    (True, False, ("customer_profile",), ("production",)),
    (True, True, (), ("production",)),
    (True, True, ("customer_profile",), ()),
])
def test_resumable_claim_refuses_unknown_or_empty_scope(has_type, has_env, types, envs):
    sql, _ = _select_next_eligible_sql(
        has_env, True, has_type, workflow_target_id="runner-production", workflow_target_revision="v1",
        execution_pool="local", allow_new_tasks=True, allow_unbound_retries=True,
        task_types=types, work_envs=envs,
    )
    assert "AND 1 = 0" in sql


@pytest.fixture
def repository_factory():
    connections = []

    def create(rows, *, has_task_type=True, has_work_env=True):
        connection = _ClaimConnection(rows)
        connections.append(connection)
        repository = MySQLTaskRepository(_ClaimDB(connection))
        repository._has_task_type_column_cache = has_task_type
        repository._has_work_env_column_cache = has_work_env
        repository._has_batch_id_column_cache = True
        return repository, connection

    yield create
    for connection in connections:
        connection.database.close()


def test_invitation_worker_claims_only_its_task_type_and_environment(repository_factory):
    repository, connection = repository_factory([
        task(1),
        task(2, task_type="invitation_strategy", work_env="test"),
        task(3, task_type="invitation_strategy"),
    ])
    claimed = repository.claim_next_queued(
        task_types=("invitation_strategy",), work_envs=("production",)
    )
    assert claimed is not None and claimed.id == 3
    assert claimed.status == "processing"
    assert connection.database.execute(
        "SELECT status FROM customer_profile_tasks WHERE id=1"
    ).fetchone()[0] == "queued"
    selected_sql, params = next(
        (sql, params) for sql, params in connection.executed if sql.startswith("SELECT")
    )
    assert "task_type IN (%s)" in selected_sql
    assert "work_env IN (%s)" in selected_sql
    assert params == ("invitation_strategy", "production")
    assert "FOR UPDATE SKIP LOCKED" in selected_sql
    assert connection.commits == 1 and connection.closed


@pytest.mark.parametrize("binding", [
    {"workflow_run_id": "runner-run"},
    {"workflow_target_id": "runner-target"},
    {"workflow_target_revision": "runner-v1"},
])
def test_legacy_dify_cannot_steal_queued_bound_task(repository_factory, binding):
    repository, connection = repository_factory([
        task(1, task_type="invitation_strategy", **binding),
        task(2, task_type="invitation_strategy"),
    ])
    claimed = repository.claim_next_queued(task_types=("invitation_strategy",))
    assert claimed is not None and claimed.id == 2
    assert connection.database.execute(
        "SELECT status FROM customer_profile_tasks WHERE id=1"
    ).fetchone()[0] == "queued"


def test_direct_default_call_keeps_all_task_types_and_environments(repository_factory):
    repository, _ = repository_factory([
        task(1, task_type="invitation_strategy", work_env="test"),
        task(2),
    ])
    claimed = repository.claim_next_queued()
    assert claimed is not None and claimed.id == 1


def test_unfiltered_legacy_metadata_still_uses_default_values(repository_factory):
    repository, _ = repository_factory([task(1)], has_task_type=False, has_work_env=False)
    claimed = repository.claim_next_queued()
    assert claimed is not None and claimed.task_type == "customer_profile" and claimed.work_env == "test"


@pytest.mark.parametrize("scope,capability", [
    ({"task_types": ("invitation_strategy",)}, {"has_task_type": False}),
    ({"work_envs": ("production",)}, {"has_work_env": False}),
])
def test_filtered_claim_fails_closed_if_scope_column_is_missing(repository_factory, scope, capability):
    repository, connection = repository_factory([task(1)], **capability)
    with pytest.raises(RuntimeError, match="run database migrations first"):
        repository.claim_next_queued(**scope)
    assert connection.executed == []
    assert connection.rollbacks == 1 and connection.closed
    assert connection.database.execute(
        "SELECT status FROM customer_profile_tasks"
    ).fetchone()[0] == "queued"


@pytest.mark.parametrize("scope", [{"task_types": ()}, {"work_envs": ()}])
def test_empty_scope_claims_nothing(repository_factory, scope):
    repository, _ = repository_factory([task(1)])
    assert repository.claim_next_queued(**scope) is None


def test_filters_remain_parameters_and_binding_guard_is_in_select_and_update(repository_factory):
    repository, connection = repository_factory([task(1)])
    claimed = repository.claim_next_queued(task_types=("customer_profile",), work_envs=("production",))
    assert claimed is not None
    queries = [sql for sql, _ in connection.executed if sql.startswith(("SELECT", "UPDATE"))]
    assert len(queries) == 2
    for sql in queries:
        assert "workflow_run_id IS NULL" in sql
        assert "workflow_target_id IS NULL" in sql
        assert "workflow_target_revision IS NULL" in sql
    sql, params = _select_next_queued_sql(True, True, True, task_types=("x' OR 1=1 --",))
    assert "x' OR 1=1" not in sql
    assert params == ("x' OR 1=1 --",)


@pytest.mark.parametrize("native,expected", [(False, 1), (True, 4)])
def test_native_invitation_claim_filters_legacy_missing_execution_and_invalid_identity(native, expected):
    rows = [task(1, task_type="invitation_strategy", batch_id="audio"),
            task(2, task_type="invitation_strategy", batch_id="audio", execution_id="bad/identity"),
            task(3, task_type="invitation_strategy", batch_id=None, execution_id="valid-3"),
            task(4, task_type="invitation_strategy", batch_id="audio", execution_id="valid-4")]
    connection = _ClaimConnection(rows)
    try:
        connection.database.create_function("REGEXP", 2, lambda pattern, value: bool(value is not None and re.search(pattern, str(value))))
        connection.database.create_function("CHAR_LENGTH", 1, lambda value: len(value) if value is not None else None)
        sql, params = _select_next_eligible_sql(True, True, True, workflow_target_id="native-invitation",
            workflow_target_revision="v1", execution_pool="local", allow_new_tasks=True, allow_unbound_retries=True,
            task_types=("invitation_strategy",), work_envs=("production",), require_invitation_identity=native)
        columns = sorted(set(re.findall(r"(?:last_attempt|local_attempt|commercial_attempt)\.(\w+)", sql)))
        connection.database.execute("CREATE TABLE customer_profile_task_attempts (" + ",".join(name + " TEXT" for name in columns) + ")")
        sqlite_sql = re.sub(r"DATE_SUB\(\s*CURRENT_TIMESTAMP,\s*INTERVAL %s SECOND\s*\)",
                            "datetime(CURRENT_TIMESTAMP, '-' || %s || ' seconds')", sql)
        selected = connection.database.execute(sqlite_sql.replace("FOR UPDATE SKIP LOCKED", "").replace("%s", "?"), params).fetchone()
        assert selected["id"] == expected
    finally:
        connection.database.close()

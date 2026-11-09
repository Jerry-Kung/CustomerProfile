"""Execute repository SQL to verify target ownership across new runs and replicas.

SQLite adapts date/locking syntax here; real SKIP LOCKED concurrency is covered
by the opt-in MySQL integration suite.
"""
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
import re
import sqlite3

import pytest

from src.domain.task.models import TaskRecord
from src.infrastructure.db.task_repository import MySQLTaskRepository


class Cursor:
    def __init__(self, db, dictionary=False):
        self.db, self.dictionary = db, dictionary
        self.result = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        if sql.strip() == "START TRANSACTION":
            self.db.execute("BEGIN")
            return
        sql = sql.replace("FOR UPDATE SKIP LOCKED", "").replace("FOR UPDATE", "")
        sql = sql.replace(" AS BINARY)", " AS BLOB)")  # Exact byte comparison on both engines.
        sql = re.sub(r"DATE_(ADD|SUB)\(\s*CURRENT_TIMESTAMP,\s*INTERVAL (%s|\d+) (SECOND|MINUTE)\s*\)",
                     lambda m: "datetime(CURRENT_TIMESTAMP, '" + ("+" if m[1] == "ADD" else "-")
                     + "' || " + m[2] + " || ' " + m[3].lower() + "s')", sql)
        sql = re.sub(r"TIMESTAMPDIFF\(\s*SECOND,", "TIMESTAMPDIFF('SECOND',", sql)
        # Recovery limits are not exercised by this adapter; SQLite lacks the
        # MySQL UPDATE ... ORDER BY alias form on some supported Python builds.
        if sql.lstrip().startswith("UPDATE") and "ORDER BY" in sql:
            sql = re.sub(r"ORDER BY .*? LIMIT %s", "", sql, flags=re.S)
            params = params[:-1]
        self.result = self.db.execute(sql.replace("%s", "?"), params)
        self.rowcount = self.result.rowcount

    def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row is not None and self.dictionary else row

    def close(self):
        pass


class Connection:
    def __init__(self, database):
        self.database = database

    def cursor(self, dictionary=False):
        return Cursor(self.database, dictionary)

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.database.rollback()

    def close(self):
        pass


@pytest.fixture
def repository():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.create_function("REGEXP", 2, lambda pattern, value: bool(re.search(pattern, value or "")))
    database.create_function("TIMESTAMPDIFF", 3, lambda unit, start, end:
                             int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
                             if start and end else None)
    integers = {"id", "generation", "attempt_count", "local_attempt_count", "commercial_attempt_count", "poll_recovery_count"}
    database.execute("CREATE TABLE customer_profile_tasks (" + ",".join(
        f.name + (" INTEGER" if f.name in integers else " TEXT") for f in fields(TaskRecord)
    ) + ", dify_run_id TEXT)")
    database.execute("""CREATE TABLE customer_profile_task_attempts (
        task_id INTEGER, generation INTEGER, attempt_no INTEGER, execution_backend TEXT,
        execution_pool TEXT, workflow_target_id TEXT, workflow_target_revision TEXT,
        retry_policy_version TEXT, route_reason TEXT, status TEXT, started_at TEXT,
        finished_at TEXT, failure_stage TEXT, error_code TEXT, error_message TEXT,
        retryable INTEGER, workflow_run_id TEXT)""")
    class DB:
        def connect(self):
            return Connection(database)
    repo = MySQLTaskRepository(DB())
    repo._has_task_type_column_cache = True
    repo._has_work_env_column_cache = True
    repo._has_batch_id_column_cache = True
    yield repo, database
    database.close()


def insert_task(db, *, task_id=91, **kwargs):
    data = asdict(TaskRecord(id=task_id, customer_phonenumber="13800138000", status="processing",
                            generation=7, claim_token="owner", workflow_target_id="qt-a",
                            workflow_target_revision="v1", batch_id="batch-1", work_env="production",
                            **kwargs))
    db.execute("INSERT INTO customer_profile_tasks (" + ",".join(data) + ") VALUES ("
               + ",".join("?" for _ in data) + ")", tuple(data.values()))
    db.commit()


def claim(repo, target, *, revision="v1", pool="local", active=True, types=("customer_profile",), envs=("production",)):
    return repo.claim_next_eligible(
        lease_seconds=180, execution_pool=pool, workflow_target_id=target,
        workflow_target_revision=revision, allow_new_tasks=active, allow_unbound_retries=active,
        task_types=types, work_envs=envs,
    )


def retry(repo, **overrides):
    args = dict(task_id=91, generation=7, claim_token="owner", error_code="KNOWN_FAILURE",
                error_stage="workflow", error_message="definite failure", delay_seconds=0,
                retain_workflow_run=False, terminal_attempt=False, attempt_no=None,
                next_execution_pool="local")
    args.update(overrides)
    return repo.schedule_retry(**args)


def insert_legacy_retry(db, *, task_id=91, pool="local", **attempt_overrides):
    insert_task(db, task_id=task_id, attempt_count=1, execution_pool=pool)
    db.execute("UPDATE customer_profile_tasks SET status='retry_wait', workflow_target_id=NULL, "
               "workflow_target_revision=NULL, claim_token=NULL, next_retry_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    attempt = dict(task_id=task_id, generation=7, attempt_no=1, status="failed", retryable=1,
                   workflow_target_id="qt-a", workflow_target_revision="v1", execution_pool=pool,
                   execution_backend=pool)
    attempt.update(attempt_overrides)
    db.execute("INSERT INTO customer_profile_task_attempts (" + ",".join(attempt) + ") VALUES ("
               + ",".join("?" for _ in attempt) + ")", tuple(attempt.values()))
    db.commit()


@pytest.mark.parametrize("pool", ("local", "commercial"))
def test_new_run_keeps_target_and_can_be_claimed_by_another_replica(repository, pool):
    repo, db = repository
    insert_task(db, workflow_run_id="finished-old-run", attempt_count=1, execution_pool=pool)
    assert retry(repo, next_execution_pool=pool)
    row = db.execute("SELECT * FROM customer_profile_tasks").fetchone()
    assert row["workflow_run_id"] is None
    assert row["workflow_target_id"] == "qt-a"
    assert row["workflow_target_revision"] == "v1"
    assert claim(repo, "runner-production", pool=pool) is None
    assert claim(repo, "qt-b", pool=pool) is None
    assert claim(repo, "qt-a", pool=pool, revision="v2") is None
    resumed = claim(repo, "qt-a", pool=pool)
    assert resumed.id == 91 and resumed.generation == 7
    assert resumed.workflow_run_id is None and resumed.claim_token != "owner"
    assert claim(repo, "qt-a", pool=pool) is None
    assert not retry(repo)  # The stale replica cannot mutate the new claim.
    assert not repo.mark_succeeded_claimed(task_id=91, generation=7, claim_token="owner",
                                         output={"stale": True}, card_id=None, nickname=None, attempt_no=None)
    assert db.execute("SELECT status FROM customer_profile_tasks").fetchone()[0] == "processing"


def test_draining_target_recovers_owned_work_even_after_scope_changes(repository):
    repo, db = repository
    insert_task(db, workflow_run_id="still-running")
    assert retry(repo, retain_workflow_run=True)
    # Consumption filters apply to new/unbound tasks; old target-owned runs drain.
    resumed = claim(repo, "qt-a", active=False, types=("invitation_strategy",), envs=("test",))
    assert resumed.id == 91 and resumed.workflow_run_id == "still-running"


def test_legacy_throughput_policy_still_releases_target(repository):
    repo, db = repository
    insert_task(db, retry_policy_version="throughput_first_v1")
    assert retry(repo, next_execution_pool="any")
    row = db.execute("SELECT * FROM customer_profile_tasks").fetchone()
    assert row["workflow_target_id"] is None and row["workflow_target_revision"] is None
    assert row["execution_pool"] == "any"


def test_expired_pre_submission_claim_keeps_single_target_identity(repository):
    repo, db = repository
    insert_task(db, lease_expires_at="2000-01-01 00:00:00")
    insert_task(db, task_id=92, lease_expires_at="2000-01-01 00:00:00")
    db.execute("UPDATE customer_profile_tasks SET workflow_target_id='qt-b' WHERE id=92")
    db.commit()
    assert repo.recover_expired_retry_leases(limit=100, workflow_target_id="qt-a", workflow_target_revision="v1") == (1, 0)
    assert db.execute("SELECT status FROM customer_profile_tasks WHERE id=92").fetchone()[0] == "processing"
    row = db.execute("SELECT * FROM customer_profile_tasks WHERE id=91").fetchone()
    assert row["status"] == "retry_wait" and row["workflow_target_id"] == "qt-a"
    assert claim(repo, "runner-production") is None
    assert claim(repo, "qt-a").id == 91


def test_heartbeat_does_not_reset_elapsed_time_and_stats_are_target_scoped(repository):
    repo, db = repository
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat(" ")
    insert_task(db, workflow_run_started_at=old, updated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" "))
    insert_task(db, task_id=92, task_type="invitation_strategy")
    db.execute("UPDATE customer_profile_tasks SET workflow_target_id='qt-b' WHERE id=92")
    db.commit()
    insert_task(db, task_id=93)
    insert_task(db, task_id=94, task_type="invitation_strategy")
    db.execute("UPDATE customer_profile_tasks SET status='queued', workflow_target_id=NULL WHERE id IN (93,94)")
    db.execute("UPDATE customer_profile_tasks SET work_env='test' WHERE id=94")
    db.commit()
    stats = repo.get_queue_stats(task_types=("invitation_strategy",),
                                work_envs=("test",), workflow_target_id="qt-a", workflow_target_revision="v1")
    # Changed scope excludes unrelated backlog, but never hides our owned run.
    assert stats["queue_depth"] == 1
    assert stats["processing_count"] == 1
    assert stats["processing_over_40m_count"] == 1
    assert stats["oldest_processing_age_seconds"] >= 3590


def test_pre_upgrade_single_target_retry_without_lineage_is_not_reassigned(repository):
    repo, db = repository
    insert_task(db, attempt_count=1)
    db.execute("UPDATE customer_profile_tasks SET status='retry_wait', workflow_target_id=NULL, "
               "workflow_target_revision=NULL, claim_token=NULL, next_retry_at=CURRENT_TIMESTAMP")
    db.commit()
    assert claim(repo, "runner-production") is None
    assert claim(repo, "qt-a") is None
    assert repo.get_queue_stats(workflow_target_id="qt-a", workflow_target_revision="v1")["unbound_single_target_retry_count"] == 1


@pytest.mark.parametrize("pool", ("local", "commercial"))
def test_legacy_retry_resumes_on_proven_owner_without_migration_or_bulk_repair(repository, pool):
    repo, db = repository
    insert_legacy_retry(db, pool=pool)
    insert_legacy_retry(db, task_id=92, pool=pool)
    before = [dict(row) for row in db.execute("SELECT * FROM customer_profile_tasks ORDER BY id")]
    assert claim(repo, "runner-production", pool=pool) is None
    assert claim(repo, "qt-b", pool=pool) is None
    assert claim(repo, "qt-a", revision="v2", pool=pool) is None
    assert claim(repo, "QT-A", pool=pool) is None
    assert claim(repo, "qt-a", revision="V1", pool=pool) is None
    stats = repo.get_queue_stats(task_types=("invitation_strategy",), work_envs=("test",),
                                workflow_target_id="qt-a", workflow_target_revision="v1")
    assert stats["retry_wait_count"] == 2  # Original owner stays visible outside the new scope.
    assert stats["unbound_single_target_retry_count"] == 0
    assert [dict(row) for row in db.execute("SELECT * FROM customer_profile_tasks ORDER BY id")] == before
    resumed = claim(repo, "qt-a", pool=pool, active=False, types=("invitation_strategy",), envs=("test",))
    assert resumed.id == 91 and resumed.workflow_target_id == "qt-a"
    assert resumed.workflow_target_revision == "v1" and resumed.workflow_run_id is None
    assert resumed.generation == 7 and resumed.attempt_count == 1
    # Normal claim updates only its task; there is no migration or sweep of other rows.
    assert dict(db.execute("SELECT * FROM customer_profile_tasks WHERE id=92").fetchone()) == before[1]
    assert db.execute("SELECT COUNT(*) FROM customer_profile_task_attempts").fetchone()[0] == 2


@pytest.mark.parametrize("attempt", [
    {"generation": 6}, {"attempt_no": 0}, {"status": "submitting"}, {"status": "abandoned"},
    {"retryable": 0}, {"workflow_target_id": None}, {"workflow_target_revision": None},
    {"workflow_target_id": ""}, {"execution_pool": "any", "execution_backend": "any"},
])
def test_legacy_retry_never_guesses_missing_or_uncertain_owner(repository, attempt):
    repo, db = repository
    insert_legacy_retry(db, **attempt)
    assert claim(repo, "qt-a") is None
    assert claim(repo, "runner-production") is None
    stats = repo.get_queue_stats(workflow_target_id="qt-a", workflow_target_revision="v1")
    assert stats["unbound_single_target_retry_count"] == 1


def test_legacy_retry_respects_backoff_and_does_not_reassign_known_run(repository):
    repo, db = repository
    insert_legacy_retry(db)
    db.execute("UPDATE customer_profile_tasks SET next_retry_at='2999-01-01 00:00:00'")
    db.commit()
    assert claim(repo, "qt-a") is None
    db.execute("UPDATE customer_profile_tasks SET next_retry_at=CURRENT_TIMESTAMP, workflow_run_id='unknown-owner-run'")
    db.commit()
    assert claim(repo, "qt-a") is None

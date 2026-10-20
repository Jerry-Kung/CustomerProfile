"""Opt-in tests against a disposable local MySQL 8 instance, without real HTTP calls."""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import replace

import pytest

from src.config.settings import MySQLConfig, WorkerConfig
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.migration_runner import run_migration
from src.infrastructure.db.task_repository import MySQLTaskRepository

pytestmark = [
    pytest.mark.mysql_integration,
    pytest.mark.skipif(os.getenv("RUN_RUNNER_MYSQL_INTEGRATION") != "1",
                       reason="requires disposable local MySQL and RUN_RUNNER_MYSQL_INTEGRATION=1"),
]
WORKER = WorkerConfig(concurrency=1, poll_interval_seconds=1, target_id="profile-runner-production",
                      task_types=("customer_profile",), work_envs=("production",))


@contextmanager
def cursor(db):
    conn = db.connect()
    cur = conn.cursor(dictionary=True)
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()
        conn.close()


@pytest.fixture
def db():
    # Deliberately fixed to loopback and an empty root password: never load deployed .env.
    admin_config = MySQLConfig(host="127.0.0.1", port=int(os.environ["RUNNER_TEST_MYSQL_PORT"]),
                               user="root", password="", database="mysql")
    admin = MySQLClient(admin_config)
    name = "runner_test_" + uuid.uuid4().hex
    with cursor(admin) as cur:
        cur.execute(f"CREATE DATABASE `{name}`")
    config = replace(admin_config, database=name)
    try:
        run_migration(config, default_contract_paths())
        run_migration(config, default_contract_paths())
        yield MySQLClient(config)
    finally:
        with cursor(admin) as cur:
            cur.execute(f"DROP DATABASE `{name}`")


def insert_task(db, suffix, *, task_type="customer_profile", work_env="production", status="failed",
                target="profile-runner-production", error_code="RUNNER_WORKFLOW_FAILED"):
    with cursor(db) as cur:
        cur.execute(
            """INSERT INTO customer_profile_tasks
               (customer_phonenumber, task_type, work_env, batch_id, status,
                workflow_target_id, workflow_target_revision, last_error_code, error_message)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'workflow failed')""",
            (f"1380000{suffix:04d}", task_type, work_env, f"batch-{suffix}", status,
             target, "v1" if target else None, error_code),
        )
        return cur.lastrowid


def test_legacy_invitation_claim_does_not_take_profile_or_bound_task(db):
    profile = insert_task(db, 1, status="queued", target=None)
    insert_task(db, 2, task_type="invitation_strategy", status="queued", target="other-target")
    invitation = insert_task(db, 3, task_type="invitation_strategy", status="queued", target=None)
    repo = MySQLTaskRepository(db)
    claimed = repo.claim_next_queued(task_types=("invitation_strategy",), work_envs=("production",))
    assert claimed.id == invitation
    assert repo.claim_next_queued(task_types=("invitation_strategy",), work_envs=("production",)) is None
    claimed = repo.claim_next_eligible(
        lease_seconds=180, execution_pool="local", workflow_target_id=WORKER.target_id,
        workflow_target_revision="v1", allow_new_tasks=True, allow_unbound_retries=True,
        task_types=WORKER.task_types, work_envs=WORKER.work_envs,
    )
    assert claimed.id == profile
    assert claimed.workflow_target_id == WORKER.target_id


def claim_target(repo, target, *, allow_new=True, allow_retries=True):
    return repo.claim_next_eligible(
        lease_seconds=180, execution_pool='local', workflow_target_id=target,
        workflow_target_revision='v1', allow_new_tasks=allow_new, allow_unbound_retries=allow_retries,
        task_types=('customer_profile',), work_envs=('production',),
        require_profile_identity=target == WORKER.target_id,
    )


def test_old_and_runner_workers_compete_for_one_task_without_duplicate_claim(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    task_id = insert_task(db, 1, status='queued', target=None)
    barrier = Barrier(2)
    def claim(target):
        barrier.wait(timeout=5)
        return claim_target(MySQLTaskRepository(db), target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['local-default', WORKER.target_id]))
    claimed = [task for task in results if task is not None]
    assert len(claimed) == 1 and claimed[0].id == task_id


def test_runner_leaves_unsupported_legacy_inputs_for_existing_worker(db):
    legacy = insert_task(db, 1, status='queued', target=None)
    valid = insert_task(db, 2, status='queued', target=None)
    with cursor(db) as cur:
        cur.execute('UPDATE customer_profile_tasks SET batch_id=NULL WHERE id=%s', (legacy,))
    repo = MySQLTaskRepository(db)
    assert claim_target(repo, WORKER.target_id).id == valid
    assert claim_target(repo, 'local-default').id == legacy


@pytest.mark.parametrize('first,second', [('local-default', WORKER.target_id), (WORKER.target_id, 'local-default')])
def test_definite_failure_can_retry_on_other_target_with_same_policy(db, first, second):
    repo = MySQLTaskRepository(db)
    original = repo.upsert_queued('13800000001', work_env='production', batch_id='business-batch')
    task = claim_target(repo, first)
    assert task.id == original.id and task.retry_policy_version == 'throughput_first_v1'
    attempt = repo.reserve_workflow_attempt(
        task_id=task.id, generation=task.generation, claim_token=task.claim_token, max_attempts=3,
        execution_pool='local', workflow_target_id=first, workflow_target_revision='v1',
        retry_policy_version=task.retry_policy_version, max_pool_attempts=3, max_target_attempts=3,
        route_reason='normal',
    )
    assert attempt == 1
    assert repo.schedule_retry(
        task_id=task.id, generation=task.generation, claim_token=task.claim_token,
        error_code='WORKFLOW_FAILED', error_stage='workflow', error_message='known failure',
        delay_seconds=0, retain_workflow_run=False, terminal_attempt=True,
        attempt_no=attempt, next_execution_pool='any',
    )
    retry = claim_target(repo, second)
    assert retry.id == task.id and retry.workflow_target_id == second
    assert retry.retry_policy_version == task.retry_policy_version
    assert retry.workflow_run_id is None and retry.attempt_count == 1


def test_runner_poll_retry_uses_original_tables_and_attempt_after_restart(db):
    with cursor(db) as cur:
        cur.execute("SHOW TABLES")
        assert {next(iter(row.values())) for row in cur.fetchall()} == {
            "customer_profile_tasks", "customer_profile_task_attempts",
        }
    repo = MySQLTaskRepository(db)
    submitted = repo.upsert_queued("13800000001", work_env="production", batch_id="business-batch")
    task = claim_target(repo, WORKER.target_id)
    attempt = repo.reserve_workflow_attempt(
        task_id=task.id, generation=task.generation, claim_token=task.claim_token, max_attempts=3,
        execution_pool="local", workflow_target_id=WORKER.target_id, workflow_target_revision="v1",
        retry_policy_version=task.retry_policy_version, max_pool_attempts=3, max_target_attempts=3,
        route_reason="normal",
    )
    assert repo.persist_workflow_run(
        task_id=task.id, generation=task.generation, claim_token=task.claim_token,
        attempt_no=attempt, workflow_run_id="runner-existing",
    )
    assert repo.schedule_retry(
        task_id=task.id, generation=task.generation, claim_token=task.claim_token,
        error_code="RUNNER_TIMEOUT", error_stage="poll", error_message="temporary timeout",
        delay_seconds=0, retain_workflow_run=True, terminal_attempt=False, attempt_no=attempt,
        increment_poll_recovery=True,
    )
    restarted = MySQLTaskRepository(db)
    assert claim_target(restarted, "local-default", allow_new=False) is None
    resumed = claim_target(restarted, WORKER.target_id, allow_new=False)
    assert resumed.id == submitted.id and resumed.generation == submitted.generation
    assert resumed.workflow_run_id == "runner-existing" and resumed.attempt_count == 1
    with cursor(db) as cur:
        cur.execute("SELECT attempt_no, workflow_run_id, status FROM customer_profile_task_attempts")
        assert cur.fetchall() == [{"attempt_no": 1, "workflow_run_id": "runner-existing", "status": "running"}]


def test_runner_recovery_does_not_touch_old_targets_and_run_stays_bound(db):
    runner_id = insert_task(db, 1, status='processing')
    old_id = insert_task(db, 2, status='processing', target='local-default')
    with cursor(db) as cur:
        cur.execute("UPDATE customer_profile_tasks SET workflow_run_id=CONCAT('run-',id), lease_expires_at=NOW()-INTERVAL 1 SECOND")
    repo = MySQLTaskRepository(db)
    assert repo.recover_expired_retry_leases(limit=100, workflow_target_id=WORKER.target_id,
                                           workflow_target_revision='v1') == (1, 0)
    with cursor(db) as cur:
        cur.execute('SELECT status FROM customer_profile_tasks WHERE id=%s', (old_id,))
        assert cur.fetchone()['status'] == 'processing'
    assert claim_target(repo, 'local-default', allow_new=False, allow_retries=False) is None
    recovered = claim_target(repo, WORKER.target_id, allow_new=False, allow_retries=False)
    assert recovered.id == runner_id and recovered.workflow_run_id == f'run-{runner_id}'


def test_scoped_recovery_does_not_abandon_old_targets_submitting_attempts(db):
    own_before = insert_task(db, 1, status='processing')
    own_unknown = insert_task(db, 2, status='processing')
    old_before = insert_task(db, 3, status='processing', target='local-default')
    old_unknown = insert_task(db, 4, status='processing', target='local-default')
    with cursor(db) as cur:
        cur.execute('UPDATE customer_profile_tasks SET lease_expires_at=NOW()-INTERVAL 1 SECOND')
        for task_id in (own_unknown, old_unknown):
            cur.execute('UPDATE customer_profile_tasks SET attempt_count=1 WHERE id=%s', (task_id,))
            cur.execute("INSERT INTO customer_profile_task_attempts (task_id,generation,attempt_no,status) VALUES (%s,1,1,'submitting')", (task_id,))
    repo = MySQLTaskRepository(db)
    assert repo.recover_expired_retry_leases(limit=100, workflow_target_id=WORKER.target_id,
                                           workflow_target_revision='v1') == (1, 1)
    with cursor(db) as cur:
        cur.execute('SELECT id,status,workflow_target_id FROM customer_profile_tasks ORDER BY id')
        rows = {row['id']: row for row in cur.fetchall()}
        assert rows[own_before]['status'] == 'retry_wait' and rows[own_before]['workflow_target_id'] is None
        assert rows[own_unknown]['status'] == 'failed'
        assert rows[old_before]['status'] == rows[old_unknown]['status'] == 'processing'
        cur.execute('SELECT task_id,status FROM customer_profile_task_attempts')
        statuses = {row['task_id']: row['status'] for row in cur.fetchall()}
        assert statuses == {own_unknown:'abandoned',old_unknown:'submitting'}


@pytest.mark.parametrize("kind", ["runner", "legacy_resumable", "legacy_queued", "commercial"])
def test_any_available_worker_can_claim_all_fresh_eligible_profiles(db, kind):
    expected = {insert_task(db, number, status="queued", target=None) for number in range(1, 101)}
    invitation = insert_task(db, 901, task_type="invitation_strategy", status="queued", target=None)
    test_task = insert_task(db, 902, work_env="test", status="queued", target=None)
    repo = MySQLTaskRepository(db)

    def claim():
        if kind == "legacy_queued":
            return repo.claim_next_queued(task_types=("customer_profile",), work_envs=("production",))
        if kind == "commercial":
            return repo.claim_next_eligible(
                lease_seconds=180, execution_pool="commercial", workflow_target_id="commercial-default",
                workflow_target_revision="v1", allow_new_tasks=True, allow_unbound_retries=True,
                task_types=("customer_profile",), work_envs=("production",),
            )
        return claim_target(repo, WORKER.target_id if kind == "runner" else "local-default")

    actual = []
    while task := claim():
        actual.append(task.id)
    assert len(actual) == len(set(actual)) == len(expected)
    assert set(actual) == expected
    assert repo.claim_next_queued(task_types=("invitation_strategy",), work_envs=("production",)).id == invitation
    assert repo.claim_next_queued(task_types=("customer_profile",), work_envs=("test",)).id == test_task


def test_workers_with_different_capacities_compete_without_duplicate_claims(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    expected = {insert_task(db, number, status="queued", target=None) for number in range(1, 61)}
    kinds = ["runner", "runner", "legacy_resumable", "legacy_queued", "commercial"]
    started = Barrier(len(kinds))

    def consume(kind):
        repo = MySQLTaskRepository(db)
        def claim():
            if kind == "legacy_queued":
                return repo.claim_next_queued(task_types=("customer_profile",), work_envs=("production",))
            if kind == "commercial":
                return repo.claim_next_eligible(
                    lease_seconds=180, execution_pool="commercial", workflow_target_id="commercial-default",
                    workflow_target_revision="v1", allow_new_tasks=True, allow_unbound_retries=True,
                    task_types=("customer_profile",), work_envs=("production",),
                )
            return claim_target(repo, WORKER.target_id if kind == "runner" else "local-default")

        import time
        started.wait(timeout=10)
        claimed = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            task = claim()
            if task is not None:
                claimed.append(task.id)
                continue
            # SKIP LOCKED may return no row while another claimant holds locks.
            # Production workers keep polling; no target has a reserved share.
            with cursor(db) as cur:
                cur.execute("SELECT COUNT(*) AS queued FROM customer_profile_tasks WHERE status='queued'")
                if cur.fetchone()["queued"] == 0:
                    return claimed
            time.sleep(0.01)
        pytest.fail("workers did not drain the queue within the deadline")

    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        groups = list(pool.map(consume, kinds))
    actual = [task_id for group in groups for task_id in group]
    assert len(actual) == len(set(actual)) == len(expected)
    assert set(actual) == expected


def test_fresh_work_is_immediately_shared_without_reassigning_a_bound_run(db):
    repo = MySQLTaskRepository(db)
    queued = insert_task(db, 1, status="queued", target=None)
    bound = insert_task(db, 2, status="retry_wait")
    with cursor(db) as cur:
        cur.execute("UPDATE customer_profile_tasks SET workflow_run_id='existing-run', next_retry_at=NOW() WHERE id=%s", (bound,))
    assert claim_target(repo, "local-default").id == queued
    assert claim_target(repo, "local-default") is None
    assert claim_target(repo, WORKER.target_id).workflow_run_id == "existing-run"


def test_closed_admission_never_claims_unbound_work_but_can_resume_own_run(db):
    repo = MySQLTaskRepository(db)
    queued = insert_task(db, 1, status="queued", target=None)
    bound = insert_task(db, 2, status="retry_wait")
    with cursor(db) as cur:
        cur.execute("UPDATE customer_profile_tasks SET workflow_run_id='existing-run', next_retry_at=NOW() WHERE id=%s", (bound,))
    resumed = claim_target(repo, WORKER.target_id, allow_new=False, allow_retries=False)
    assert resumed.id == bound and resumed.workflow_run_id == "existing-run"
    assert claim_target(repo, WORKER.target_id, allow_new=False, allow_retries=False) is None
    assert claim_target(repo, "local-default").id == queued

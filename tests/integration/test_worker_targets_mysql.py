"""Isolated real-MySQL checks for concurrent claims and retry target fencing.

Opt in with RUN_WORKER_MYSQL_INTEGRATION=1 and WORKER_TEST_MYSQL_*.
Only a loopback server is accepted. Every test creates/drops its own random DB;
application MYSQL_* and repository .env files are deliberately never loaded.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import mysql.connector
import pytest

from src.config import settings
from src.config.settings import MigrationConfig, MySQLConfig
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.migration_runner import run_migration
from src.infrastructure.db.task_repository import MySQLTaskRepository

pytestmark = [
    pytest.mark.worker_mysql_integration,
    pytest.mark.skipif(os.getenv("RUN_WORKER_MYSQL_INTEGRATION") != "1",
                       reason="requires explicitly configured disposable local MySQL"),
]


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: None)
    host = os.getenv("WORKER_TEST_MYSQL_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Worker integration tests accept only a disposable loopback MySQL")
    connection_args = dict(host=host, port=int(os.getenv("WORKER_TEST_MYSQL_PORT", "3307")),
                           user=os.getenv("WORKER_TEST_MYSQL_USER", "root"),
                           password=os.getenv("WORKER_TEST_MYSQL_PASSWORD", ""), connection_timeout=5)
    database = "codex_worker_test_" + uuid.uuid4().hex
    admin = mysql.connector.connect(**connection_args)
    try:
        admin.cursor().execute(f"CREATE DATABASE `{database}`")
        config = MySQLConfig(host=host, port=connection_args["port"], user=connection_args["user"],
                             password=connection_args["password"], database=database)
        run_migration(config, default_contract_paths(), wait=MigrationConfig(1, 0))
        repo = MySQLTaskRepository(MySQLClient(config), default_retry_policy_version="single_target_v1")
        yield repo, config
    finally:
        admin.cursor().execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()


def claim(repo, target, *, types=("customer_profile",), envs=("production",), active=True):
    return repo.claim_next_eligible(lease_seconds=180, execution_pool="local", workflow_target_id=target,
                                   workflow_target_revision="v1", allow_new_tasks=active,
                                   allow_unbound_retries=active, task_types=types, work_envs=envs)


def submit(repo, index, task_type="customer_profile", work_env="production"):
    return repo.upsert_queued(f"138{index:08d}", task_type=task_type, work_env=work_env,
                              batch_id=f"isolated-{index}")


def test_parallel_targets_and_replicas_claim_each_task_once(repository):
    repo, _ = repository
    tasks = [submit(repo, i) for i in range(1, 13)]
    targets = ["qt-a", "qt-a", "qt-b", "runner-production"] * 3
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda target: claim(repo, target), targets))
    assert all(task is not None for task in claimed)
    assert {task.id for task in claimed} == {task.id for task in tasks}
    assert len({task.claim_token for task in claimed}) == len(tasks)
    assert claim(repo, "qt-a") is None


def test_failed_run_stays_on_target_and_old_claim_cannot_overwrite(repository):
    repo, _ = repository
    submit(repo, 1)
    owned = claim(repo, "qt-a")
    attempt = repo.reserve_workflow_attempt(task_id=owned.id, generation=owned.generation,
        claim_token=owned.claim_token, max_attempts=3, execution_pool="local", workflow_target_id="qt-a",
        workflow_target_revision="v1", retry_policy_version="single_target_v1",
        max_pool_attempts=3, max_target_attempts=3, route_reason="single_target")
    assert repo.persist_workflow_run(task_id=owned.id, generation=owned.generation,
                                    claim_token=owned.claim_token, attempt_no=attempt, workflow_run_id="definitely-failed")
    assert repo.schedule_retry(task_id=owned.id, generation=owned.generation, claim_token=owned.claim_token,
        error_code="KNOWN_FAILURE", error_stage="workflow", error_message="known failure", delay_seconds=0,
        retain_workflow_run=False, terminal_attempt=True, attempt_no=attempt, next_execution_pool="local")
    assert claim(repo, "runner-production") is None
    assert claim(repo, "qt-b") is None
    resumed = claim(repo, "qt-a", active=False)
    assert resumed.id == owned.id and resumed.workflow_run_id is None
    assert resumed.claim_token != owned.claim_token
    assert not repo.mark_succeeded_claimed(task_id=owned.id, generation=owned.generation,
        claim_token=owned.claim_token, output={"stale": True}, card_id=None, nickname=None, attempt_no=attempt)
    assert repo.mark_succeeded_claimed(task_id=resumed.id, generation=resumed.generation,
        claim_token=resumed.claim_token, output={"current": True}, card_id=None, nickname=None, attempt_no=attempt)


def test_scope_filters_and_replay_safe_upgrade_restore_old_retry(repository):
    repo, config = repository
    submit(repo, 1, task_type="invitation_strategy")
    submit(repo, 2, work_env="test")
    task = submit(repo, 3)
    owned = claim(repo, "qt-a")
    assert owned.id == task.id
    attempt = repo.reserve_workflow_attempt(task_id=owned.id, generation=owned.generation,
        claim_token=owned.claim_token, max_attempts=3, execution_pool="local", workflow_target_id="qt-a",
        workflow_target_revision="v1", retry_policy_version="single_target_v1",
        max_pool_attempts=3, max_target_attempts=3, route_reason="single_target")
    connection = MySQLClient(config).connect()
    try:
        cursor = connection.cursor()
        cursor.execute("UPDATE customer_profile_tasks SET status='retry_wait', workflow_target_id=NULL, "
                       "workflow_target_revision=NULL, claim_token=NULL, next_retry_at=CURRENT_TIMESTAMP WHERE id=%s", (owned.id,))
        cursor.execute("UPDATE customer_profile_task_attempts SET status='failed', retryable=1 "
                       "WHERE task_id=%s AND generation=%s AND attempt_no=%s",
                       (owned.id, owned.generation, attempt))
        connection.commit()
    finally:
        connection.close()
    assert claim(repo, "runner-production") is None
    for _ in range(2):
        run_migration(config, default_contract_paths(), wait=MigrationConfig(1, 0))
    assert claim(repo, "runner-production") is None
    assert claim(repo, "qt-a").id == owned.id

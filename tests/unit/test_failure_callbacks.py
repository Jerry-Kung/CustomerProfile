"""Real persistence/restart tests; the small DB adapter translates MySQL syntax.

SQLite does not exercise MySQL's SKIP LOCKED behavior. Concurrency/locking still
requires the opt-in MySQL deployment checks.
"""

import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from src.config.settings import FailureCallbackConfig, _failure_callback_config
from src.domain.task.failure_callback import ProfileFailure
from src.infrastructure.db.failure_callback_repository import MySQLFailureCallbackRepository
from src.infrastructure.workflow.failure_callback import ProfileFailureClient
from src.worker.failure_callbacks import FailureCallbackDispatcher
from src.worker.runner import WorkerRunner


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def execute(self, sql, params=()):
        sql = sql.replace("START TRANSACTION", "BEGIN")
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE")
        sql = sql.replace(" FOR UPDATE SKIP LOCKED", "")
        sql = sql.replace("DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND)", "datetime('now', '+' || %s || ' seconds')")
        self.cursor.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None


class Connection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def cursor(self, dictionary=False):
        return Cursor(self.connection)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "failure-outbox.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE customer_profile_tasks (
                id INTEGER PRIMARY KEY, generation INTEGER DEFAULT 1,
                customer_phonenumber TEXT DEFAULT '13800000000', batch_id TEXT DEFAULT 'batch-1',
                work_env TEXT DEFAULT 'production', status TEXT DEFAULT 'failed',
                task_type TEXT DEFAULT 'customer_profile', last_error_code TEXT,
                execution_id TEXT,
                error_message TEXT DEFAULT 'confirmed failure'
            );
            CREATE TABLE customer_profile_failure_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, generation INTEGER,
                customer_phone TEXT, batch_id TEXT, work_env TEXT, error_message TEXT,
                task_type TEXT DEFAULT 'customer_profile', execution_id TEXT,
                attempts INTEGER DEFAULT 0, next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT, UNIQUE(task_id, generation)
            );
        """)
    return path


def repository(path):
    class Client:
        def connect(self):
            return Connection(path)
    return MySQLFailureCallbackRepository(Client(), retry_seconds=0)


def config():
    return FailureCallbackConfig(
        enabled=True, base_url_test="https://test.example.test", api_key_test="test-key",
        base_url_production="https://prod.example.test", api_key_production="production-key",
    )


def test_failed_http_delivery_survives_restart_and_is_deduplicated(database):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO customer_profile_tasks(id) VALUES (1)")
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200, json={"msg": "ok"})

    client = ProfileFailureClient(config(), transport=httpx.MockTransport(endpoint))
    dispatcher = FailureCallbackDispatcher(repository(database), client)
    with pytest.raises(httpx.HTTPStatusError):
        dispatcher.run_once()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT attempts, delivered_at FROM customer_profile_failure_outbox").fetchone() == (1, None)
    # Rebuild every component, as on a worker restart.
    resumed = FailureCallbackDispatcher(repository(database), client)
    assert resumed.run_once()
    assert not resumed.run_once()
    assert len(requests) == 2
    assert json.loads(requests[0].content) == {
        "phone": "13800000000", "batch_id": "batch-1", "error_msg": "confirmed failure",
    }
    assert requests[0].url == "https://prod.example.test/api/v1/remote/callback/analysis-failed"
    assert requests[0].headers["X-API-Key"] == "production-key"


@pytest.mark.parametrize("status,code,task_type,expected", [
    ("processing", None, "customer_profile", 0),
    ("retry_wait", "RUNNER_POLL_TIMEOUT", "customer_profile", 0),
    ("succeeded", None, "customer_profile", 0),
    ("failed", "RUNNER_SUBMIT_UNKNOWN", "customer_profile", 0),
    ("failed", "QINGTIAN_SUBMIT_UNKNOWN", "customer_profile", 0),
    ("failed", "RUNNER_RUN_STALLED", "customer_profile", 0),
    ("failed", "RUNNER_REQUIRES_RECONCILIATION", "customer_profile", 0),
    ("failed", "RUNNER_RUN_FAILED", "invitation_strategy", 0),
    ("failed", "RUNNER_RUN_FAILED", "customer_profile", 1),
    ("failed", "QINGTIAN_RETRY_EXHAUSTED", "customer_profile", 1),
])
def test_only_confirmed_terminal_profile_failures_are_enqueued(database, status, code, task_type, expected):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO customer_profile_tasks(id,status,last_error_code,task_type) VALUES (1,?,?,?)", (status, code, task_type))
    repository(database).enqueue_failed()
    repository(database).enqueue_failed()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM customer_profile_failure_outbox").fetchone()[0] == expected


def test_new_generation_keeps_its_own_failure_receipt(database):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO customer_profile_tasks(id) VALUES (1)")
    store = repository(database)
    store.enqueue_failed()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE customer_profile_tasks SET generation=2, batch_id='batch-2'")
    store.enqueue_failed()
    batches = []
    assert store.deliver_next(lambda failure: batches.append(failure.batch_id))
    assert store.deliver_next(lambda failure: batches.append(failure.batch_id))
    assert batches == ["batch-1", "batch-2"]


@pytest.mark.parametrize("message", [
    "DIFY_TIMEOUT: polling workflow result timed out",
    "DIFY_REQUEST_FAILED: connection dropped before acknowledgement",
    "WORKER_UNEXPECTED_ERROR: database write failed after remote execution",
])
def test_legacy_unknown_outcomes_do_not_send_terminal_failure(database, message):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO customer_profile_tasks(id,error_message) VALUES (1,?)", (message,))
    repository(database).enqueue_failed()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM customer_profile_failure_outbox").fetchone()[0] == 0


def test_callback_client_selects_test_environment_and_retries_http_failure():
    seen = []
    client = ProfileFailureClient(config(), transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(401),
    ))
    with pytest.raises(httpx.HTTPStatusError):
        client.send(ProfileFailure("13800000000", "b", "test", "failed"))
    assert seen[0].url.host == "test.example.test"
    assert seen[0].headers["X-API-Key"] == "test-key"
    with pytest.raises(ValueError):
        client.send(ProfileFailure("13800000000", "b", "unknown", "failed"))


def test_enabled_callback_requires_url_and_key(monkeypatch):
    monkeypatch.setenv("PROFILE_FAILURE_CALLBACK_ENABLED", "true")
    monkeypatch.delenv("PROFILE_API_BASE_URL", raising=False)
    monkeypatch.delenv("PROFILE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="PROFILE_API_BASE_URL"):
        _failure_callback_config()
    monkeypatch.setenv("PROFILE_API_BASE_URL", "https://profile.example.test")
    with pytest.raises(ValueError, match="PROFILE_API_KEY"):
        _failure_callback_config()
    monkeypatch.setenv("PROFILE_API_KEY", "test-key")
    assert _failure_callback_config().enabled


def test_worker_starts_and_stops_failure_delivery_independently():
    started = threading.Event()
    stopped = threading.Event()

    class Dispatcher:
        def run(self, stop_event):
            started.set()
            stop_event.wait()
            stopped.set()

    worker = WorkerRunner(
        SimpleNamespace(execute_once=lambda: "idle", request_shutdown=lambda: None),
        interval_seconds=0.01, failure_dispatcher=Dispatcher(),
    )
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=2)
    finally:
        worker.stop()
        thread.join(timeout=2)
    assert stopped.is_set()
    assert not thread.is_alive()


@pytest.mark.parametrize("status,code,execution_id,expected", [
    ("failed", "QINGTIAN_RETRY_EXHAUSTED", "exec-1", 1),
    ("failed", "QINGTIAN_SUBMIT_UNKNOWN", "exec-1", 0),
    ("failed", "QINGTIAN_RUN_STALLED", "exec-1", 0),
    ("failed", "QINGTIAN_REQUIRES_RECONCILIATION", "exec-1", 0),
    ("failed", "WORKER_UNEXPECTED_ERROR", "exec-1", 0),
    ("failed", "INVITATION_WRITEBACK_MISSING", "exec-1", 1),
    ("failed", "INVITATION_RUNNER_WORKFLOW_FAILED", "exec-1", 1),
    ("failed", "INVITATION_RUNNER_SUBMIT_UNKNOWN", "exec-1", 0),
    ("failed", "INVITATION_RUNNER_REQUIRES_RECONCILIATION", "exec-1", 0),
    ("failed", "INVITATION_RUNNER_RUN_STALLED", "exec-1", 0),
    ("retry_wait", "QINGTIAN_REQUEST_FAILED", "exec-1", 0),
    ("processing", None, "exec-1", 0),
    ("failed", "QINGTIAN_RETRY_EXHAUSTED", None, 0),
])
def test_invitation_delivery_only_claims_identified_final_failures(database, status, code, execution_id, expected):
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO customer_profile_tasks(id,task_type,status,last_error_code,execution_id) VALUES (1,'invitation_strategy',?,?,?)", (status, code, execution_id))
    store = repository(database)
    store.enqueue_failed()
    store.enqueue_failed()
    seen = []
    client = ProfileFailureClient(config(), transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200),
    ))
    assert store.deliver_next(client.send) == bool(expected)
    if expected:
        assert seen[0].url.path == "/api/v1/remote/callback/invitation-failed"
        assert json.loads(seen[0].content)["execution_id"] == execution_id
        assert json.loads(seen[0].content)["final"] is True

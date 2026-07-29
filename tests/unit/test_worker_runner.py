from __future__ import annotations

import logging
import threading
import time

from src.worker.runner import WorkerRunner


class _Executor:
    def __init__(self, status: str):
        self.status = status
        self.shutdown_requests = 0

    def execute_once(self) -> str:
        return self.status

    def request_shutdown(self) -> None:
        self.shutdown_requests += 1


def test_worker_run_once_idle_logged_at_debug_only(caplog):
    runner = WorkerRunner(executor=_Executor("idle"), interval_seconds=0.01)
    with caplog.at_level(logging.DEBUG, logger="worker"):
        runner.run_once()
    assert any("worker_tick status=idle" in record.getMessage() for record in caplog.records)


def test_worker_run_once_claimed_logs(caplog):
    runner = WorkerRunner(executor=_Executor("succeeded"), interval_seconds=0.01)
    runner.run_once()
    assert any("worker_tick status=succeeded" in record.getMessage() for record in caplog.records)


def test_worker_can_start_and_stop():
    executor = _Executor("idle")
    runner = WorkerRunner(executor=executor, interval_seconds=0.02)
    t = threading.Thread(target=runner.run_forever, daemon=True)
    t.start()
    time.sleep(0.06)
    runner.stop()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert executor.shutdown_requests >= 1


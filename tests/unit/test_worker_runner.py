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



def test_free_worker_slots_claim_independently_up_to_configured_concurrency():
    concurrency = 3
    lock = threading.Lock()
    all_slots_busy = threading.Event()
    release_tasks = threading.Event()
    active = 0
    peak = 0
    calls = 0

    class BlockingExecutor:
        def execute_once(self):
            nonlocal active, peak, calls
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == concurrency:
                    all_slots_busy.set()
            try:
                assert release_tasks.wait(timeout=5)
                return "succeeded"
            finally:
                with lock:
                    active -= 1

    runner = WorkerRunner(executor=BlockingExecutor(), concurrency=concurrency,
                          interval_seconds=0.01)
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    try:
        assert all_slots_busy.wait(timeout=5)
        with lock:
            assert calls == concurrency
            assert active == peak == concurrency
        runner.stop()
    finally:
        runner.stop()
        release_tasks.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert active == 0
    assert calls == concurrency


def test_maintenance_continues_while_all_execution_slots_are_busy():
    entered, maintenance, release = threading.Event(), threading.Event(), threading.Event()

    class BusyExecutor:
        def execute_once(self):
            entered.set()
            release.wait(timeout=3)
            return "succeeded"

        def recover_once(self):
            maintenance.set()

    runner = WorkerRunner(BusyExecutor(), concurrency=1, supervisor_interval_seconds=0.01)
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=1)
        assert maintenance.wait(timeout=1)
    finally:
        runner.stop()
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()

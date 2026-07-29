from __future__ import annotations

import threading
import time

from src.config.logging_utils import get_logger
from src.worker.executor import WorkerExecutor


class WorkerRunner:
    def __init__(
        self,
        executor: WorkerExecutor,
        interval_seconds: float = 2.0,
        *,
        concurrency: int = 1,
        supervisor_interval_seconds: float = 60.0,
        summary_interval_seconds: float = 300.0,
    ):
        self._executor = executor
        self._interval_seconds = interval_seconds
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._concurrency = concurrency
        self._supervisor_interval_seconds = supervisor_interval_seconds
        self._summary_interval_seconds = summary_interval_seconds
        self._stop_event = threading.Event()
        self._logger = get_logger("worker")
        self._threads: list[threading.Thread] = []
        self._heartbeats: dict[str, float] = {}
        self._heartbeats_lock = threading.Lock()
        self._supervisor_thread: threading.Thread | None = None
        self._stats_lock = threading.Lock()
        self._stats = {
            "idle": 0,
            "succeeded": 0,
            "retrying": 0,
            "failed": 0,
            "error": 0,
            "execute_count": 0,
            "execute_elapsed_ms": 0,
        }
        self._inflight = 0
        self._last_summary = time.monotonic()

    def run_once(self) -> None:
        start = time.perf_counter()
        with self._stats_lock:
            self._inflight += 1
        try:
            status = self._executor.execute_once()
        except Exception:
            with self._stats_lock:
                self._inflight -= 1
                self._stats["error"] += 1
                self._stats["execute_count"] += 1
                self._stats["execute_elapsed_ms"] += int((time.perf_counter() - start) * 1000)
            raise
        with self._stats_lock:
            self._inflight -= 1
            self._stats[status] = self._stats.get(status, 0) + 1
            self._stats["execute_count"] += 1
            self._stats["execute_elapsed_ms"] += int((time.perf_counter() - start) * 1000)
        # Avoid INFO spam every poll interval when there is no queued work.
        if status == "idle":
            self._logger.debug("worker_tick status=idle")
        else:
            self._logger.info("worker_tick status=%s", status)
        self._maybe_log_summary()

    def _maybe_log_summary(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._stats_lock:
            if not force and now - self._last_summary < self._summary_interval_seconds:
                return
            stats = dict(self._stats)
            inflight = self._inflight
            self._stats = {
                "idle": 0,
                "succeeded": 0,
                "retrying": 0,
                "failed": 0,
                "error": 0,
                "execute_count": 0,
                "execute_elapsed_ms": 0,
            }
            interval_seconds = int(now - self._last_summary)
            self._last_summary = now
        snapshot = {}
        snapshot_fn = getattr(self._executor, "operational_snapshot", None)
        if callable(snapshot_fn):
            try:
                snapshot = snapshot_fn()
            except Exception:
                self._logger.exception("worker_operational_snapshot_failed")
        self._logger.info(
            "worker_runtime_summary 说明=Worker队列运行概览 interval_seconds=%s inflight_threads=%s idle_polls=%s cycle_succeeded=%s cycle_retrying=%s cycle_failed=%s cycle_errors=%s average_execute_elapsed_ms=%s queued_count=%s retry_wait_count=%s processing_count=%s total_succeeded=%s total_failed=%s oldest_queued_age_seconds=%s oldest_processing_age_seconds=%s processing_over_40m_count=%s target_circuit_state=%s target_consecutive_failures=%s target_circuit_remaining_seconds=%s",
            interval_seconds,
            inflight,
            stats.get("idle", 0),
            stats.get("succeeded", 0),
            stats.get("retrying", 0),
            stats.get("failed", 0),
            stats.get("error", 0),
            (
                int(stats.get("execute_elapsed_ms", 0) / stats.get("execute_count", 1))
                if stats.get("execute_count", 0)
                else None
            ),
            snapshot.get("queue_depth"),
            snapshot.get("retry_wait_count"),
            snapshot.get("processing_count"),
            snapshot.get("succeeded_count"),
            snapshot.get("failed_count"),
            snapshot.get("oldest_queued_age_seconds"),
            snapshot.get("oldest_processing_age_seconds"),
            snapshot.get("processing_over_40m_count"),
            snapshot.get("target_circuit_state"),
            snapshot.get("target_consecutive_failures"),
            snapshot.get("target_circuit_remaining_seconds"),
        )

    def _loop(self, thread_name: str = "worker") -> None:
        while not self._stop_event.is_set():
            with self._heartbeats_lock:
                self._heartbeats[thread_name] = time.monotonic()
            try:
                self.run_once()
            except Exception:
                # P0 outer guard: catches anything that escaped execute_once's own handlers
                # (including failures inside those handlers themselves). The thread MUST survive.
                self._logger.exception(
                    "worker_loop_unexpected_error thread=%s", thread_name,
                )
            self._stop_event.wait(self._interval_seconds)

    def _spawn_worker(self, index: int) -> threading.Thread:
        name = f"worker-{index}"
        with self._heartbeats_lock:
            self._heartbeats[name] = time.monotonic()
        t = threading.Thread(target=self._loop, name=name, args=(name,), daemon=False)
        t.start()
        return t

    def _supervisor_loop(self) -> None:
        while True:
            # wait() returns True iff stop_event was set; that is our exit signal.
            if self._stop_event.wait(self._supervisor_interval_seconds):
                return
            try:
                self._check_and_respawn()
                self._maybe_log_summary()
            except Exception:
                self._logger.exception("worker_supervisor_check_failed")

    def _check_and_respawn(self) -> None:
        alive_after = 0
        respawned: list[str] = []
        for i in range(len(self._threads)):
            t = self._threads[i]
            if t.is_alive():
                alive_after += 1
                continue
            old_name = t.name
            self._logger.warning(
                "worker_thread_respawned name=%s reason=died_silently",
                old_name,
            )
            new_t = self._spawn_worker(i)
            self._threads[i] = new_t
            respawned.append(old_name)
            alive_after += 1
        now = time.monotonic()
        with self._heartbeats_lock:
            heartbeats_snapshot = dict(self._heartbeats)
        stale = [
            name
            for name, ts in heartbeats_snapshot.items()
            if now - ts > self._supervisor_interval_seconds * 5
        ]
        # Demote the steady-state "all alive, nothing stale" heartbeat to DEBUG so it does not
        # flood production INFO logs every supervisor tick. Anything actionable stays at INFO.
        if respawned or stale or alive_after != self._concurrency:
            self._logger.info(
                "worker_heartbeat alive=%s/%s respawned=%s stale=%s",
                alive_after,
                self._concurrency,
                respawned,
                stale,
            )
        else:
            self._logger.debug(
                "worker_heartbeat alive=%s/%s respawned=%s stale=%s",
                alive_after,
                self._concurrency,
                respawned,
                stale,
            )

    def run_forever(self) -> None:
        self._logger.info("worker_started concurrency=%s", self._concurrency)
        self._maybe_log_summary(force=True)
        self._threads = [self._spawn_worker(i) for i in range(self._concurrency)]
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="worker-supervisor",
            daemon=False,
        )
        self._supervisor_thread.start()
        try:
            # Park the main thread on stop_event; supervisor keeps workers alive in between.
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)
        except KeyboardInterrupt:
            self._logger.info("worker_shutdown_requested")
        finally:
            self._stop_event.set()
            # supervisor will observe stop_event and exit; it will not respawn anything from here on.
            for t in list(self._threads):
                t.join(timeout=30.0)
            if self._supervisor_thread is not None:
                self._supervisor_thread.join(timeout=10.0)
            self._maybe_log_summary(force=True)
            self._logger.info("worker_stopped")

    def stop(self) -> None:
        self._stop_event.set()

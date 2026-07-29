from __future__ import annotations

import signal

from src.app.bootstrap import bootstrap
from src.config.logging_utils import get_logger, log_runtime_identity
from src.worker.runner import WorkerRunner


def _install_shutdown_signal_handlers(runner: WorkerRunner) -> None:
    logger = get_logger("worker")

    def handle_shutdown(signum, _frame) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)
        logger.info("worker_shutdown_signal_received signal=%s", signal_name)
        runner.stop()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


def main() -> int:
    config, _, _, executor, _ = bootstrap("worker")
    log_runtime_identity(get_logger("worker"), component="worker", app_env=config.app_env)
    runner = WorkerRunner(
        executor=executor,
        interval_seconds=config.worker.poll_interval_seconds,
        concurrency=config.worker.concurrency,
    )
    _install_shutdown_signal_handlers(runner)
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


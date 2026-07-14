from __future__ import annotations

from src.app.bootstrap import bootstrap
from src.config.logging_utils import get_logger, log_runtime_identity
from src.worker.runner import WorkerRunner


def main() -> int:
    config, _, _, executor, _ = bootstrap()
    log_runtime_identity(get_logger("worker"), component="worker", app_env=config.app_env)
    runner = WorkerRunner(
        executor=executor,
        interval_seconds=config.worker.poll_interval_seconds,
        concurrency=config.worker.concurrency,
    )
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


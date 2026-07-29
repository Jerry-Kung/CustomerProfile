from __future__ import annotations

import signal

from src.app.run_worker import _install_shutdown_signal_handlers


class _Runner:
    def __init__(self):
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def test_sigterm_requests_worker_shutdown(monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )
    runner = _Runner()

    _install_shutdown_signal_handlers(runner)
    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers
    assert runner.stop_calls == 1

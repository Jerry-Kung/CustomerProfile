from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import src.infrastructure.db.task_repository as task_repository
from src.infrastructure.db.task_repository import MySQLTaskRepository


def test_column_capability_probe_is_serialized_and_cached(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def fake_column_exists(_conn, column_name: str) -> bool:
        nonlocal calls
        assert column_name == "work_env"
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return True

    monkeypatch.setattr(task_repository, "_column_exists", fake_column_exists)
    repository = MySQLTaskRepository(db_client=object())

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda _: repository._has_work_env_column(object()),
                range(20),
            )
        )

    assert results == [True] * 20
    assert calls == 1

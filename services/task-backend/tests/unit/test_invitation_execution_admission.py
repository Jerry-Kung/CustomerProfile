"""An acknowledged invitation execution must never create another generation."""

from types import SimpleNamespace

import pytest

from src.domain.task.errors import TaskExecutionConflictError
from src.infrastructure.db.task_repository import MySQLTaskRepository


class Cursor:
    def __init__(self, record):
        self.record = record
        self.result = None

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT GET_LOCK"):
            self.result = {"acquired": 1}
        elif compact.startswith("SELECT"):
            self.result = self.record
        elif compact.startswith("INSERT"):
            pytest.fail("the accepted execution was resubmitted")

    def fetchone(self):
        return self.result


@pytest.mark.parametrize("status", ["queued", "processing", "retry_wait", "succeeded", "failed"])
def test_same_execution_admission_is_idempotent_even_after_completion(status):
    record = {"id": 7, "generation": 3, "customer_phonenumber": "13800000000",
              "status": status, "execution_id": "same-execution", "batch_id": "audio", "work_env": "production"}
    cursor = Cursor(record)
    connection = SimpleNamespace(cursor=lambda **_: cursor, commit=lambda: None,
                                 rollback=lambda: None, close=lambda: None)
    repository = MySQLTaskRepository(SimpleNamespace(connect=lambda: connection))
    for attr in ("work_env", "batch_id", "task_type"):
        setattr(repository, f"_has_{attr}_column_cache", True)
    # Capacity is irrelevant for retrying an already accepted execution.
    repository.count_queued = lambda **_: pytest.fail("duplicate admission checked queue capacity")
    result = repository.upsert_queued("13800000000", task_type="invitation_strategy",
                                     work_env="production", batch_id="audio", execution_id="same-execution")
    assert (result.id, result.generation, result.status, result.execution_id) == (7, 3, status, "same-execution")
    with pytest.raises(TaskExecutionConflictError):
        repository.upsert_queued("13800000000", task_type="invitation_strategy",
                                 work_env="production", batch_id="different", execution_id="same-execution")

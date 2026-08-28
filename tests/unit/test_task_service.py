from __future__ import annotations

import logging

from src.domain.task.models import TaskRecord
from src.domain.task.service import TaskService


class InMemoryTaskRepository:
    def __init__(self):
        self._rows: dict[tuple[str, str], TaskRecord] = {}
        self._id_seed = 1

    def upsert_queued(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> TaskRecord:
        key = (customer_phonenumber, task_type)
        existing = self._rows.get(key)
        if existing is None:
            existing = TaskRecord(
                id=self._id_seed,
                customer_phonenumber=customer_phonenumber,
                status="queued",
                task_type=task_type,
            )
            self._id_seed += 1
        existing.status = "queued"
        existing.output = None
        existing.card_id = None
        existing.nickname = None
        existing.error_message = None
        existing.work_env = work_env
        existing.batch_id = batch_id
        existing.task_type = task_type
        self._rows[key] = existing
        return existing

    def claim_next_queued(self) -> TaskRecord | None:
        queued = [v for v in self._rows.values() if v.status == "queued"]
        if not queued:
            return None
        queued.sort(key=lambda x: x.id or 0)
        row = queued[0]
        row.status = "processing"
        return row

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int) -> int:
        affected = 0
        for row in self._rows.values():
            if row.status == "processing" and affected < limit:
                row.status = "queued"
                row.error_message = "PROCESSING_TIMEOUT_RECOVERED"
                affected += 1
        return affected

    def list_by_prefix(
        self,
        *,
        phone_prefix: str,
        page: int,
        page_size: int,
        task_type: str | None = None,
    ):
        items = [
            v
            for v in self._rows.values()
            if v.customer_phonenumber.startswith(phone_prefix)
            and (task_type is None or v.task_type == task_type)
        ]
        items.sort(key=lambda x: x.id or 0, reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    def get_by_phone(self, customer_phonenumber: str, *, task_type: str = "customer_profile"):
        return self._rows.get((customer_phonenumber, task_type))

    def count_queued(self, *, task_type: str | None = None) -> int:
        return sum(
            1
            for v in self._rows.values()
            if v.status == "queued" and (task_type is None or v.task_type == task_type)
        )


def _logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.addHandler(logging.StreamHandler())
    logger.setLevel("INFO")
    return logger


def test_submit_task_is_idempotent_and_resets_fields():
    repo = InMemoryTaskRepository()
    service = TaskService(repository=repo, logger=_logger("test_task_service"))

    first = service.submit_task("13800138000")
    first.output = {"x": 1}
    first.card_id = "card"
    first.nickname = "nick"
    first.error_message = "err"

    second = service.submit_task("13800138000")
    assert second.id == first.id
    assert second.status == "queued"
    assert second.output is None
    assert second.card_id is None
    assert second.nickname is None
    assert second.error_message is None


def test_submit_task_persists_work_env_and_batch_id():
    repo = InMemoryTaskRepository()
    service = TaskService(repository=repo, logger=_logger("test_task_service_meta"))
    record = service.submit_task(
        "13800138000",
        work_env="production",
        batch_id="2026Q2-001",
    )
    assert record.work_env == "production"
    assert record.batch_id == "2026Q2-001"
    assert record.task_type == "customer_profile"


def test_submit_task_two_types_for_same_phone_coexist():
    repo = InMemoryTaskRepository()
    service = TaskService(repository=repo, logger=_logger("test_task_service_types"))
    a = service.submit_task("13800138000", task_type="customer_profile")
    b = service.submit_task("13800138000", task_type="invitation_strategy")
    assert a.id != b.id
    assert a.task_type == "customer_profile"
    assert b.task_type == "invitation_strategy"

    fetched_a = service.get_task_by_phone("13800138000", task_type="customer_profile")
    fetched_b = service.get_task_by_phone("13800138000", task_type="invitation_strategy")
    assert fetched_a is not None and fetched_a.id == a.id
    assert fetched_b is not None and fetched_b.id == b.id


def test_claim_next_task_moves_status_to_processing():
    repo = InMemoryTaskRepository()
    logger = _logger("test_claim")
    service = TaskService(repository=repo, logger=logger)
    service.submit_task("13800138000")
    service.submit_task("13900139000")

    claimed = service.claim_next_task()
    assert claimed is not None
    assert claimed.customer_phonenumber == "13800138000"
    assert claimed.status == "processing"
    assert claimed.task_type == "customer_profile"


def test_requeue_stale_processing():
    repo = InMemoryTaskRepository()
    logger = _logger("test_requeue")
    service = TaskService(repository=repo, logger=logger)
    service.submit_task("13800138000")
    claimed = service.claim_next_task()
    assert claimed is not None and claimed.status == "processing"

    affected = service.requeue_stale_processing(timeout_seconds=60, limit=10)
    assert affected == 1
    row = service.get_task_by_phone("13800138000")
    assert row is not None
    assert row.status == "queued"


def test_list_tasks_filters_by_task_type():
    repo = InMemoryTaskRepository()
    service = TaskService(repository=repo, logger=_logger("test_list_filter"))
    service.submit_task("13800138000", task_type="customer_profile")
    service.submit_task("13800138000", task_type="invitation_strategy")

    items_all, total_all = service.list_tasks(phone_prefix="138", page=1, page_size=20)
    assert total_all == 2
    assert len(items_all) == 2

    items_inv, total_inv = service.list_tasks(
        phone_prefix="138", page=1, page_size=20, task_type="invitation_strategy"
    )
    assert total_inv == 1
    assert items_inv[0].task_type == "invitation_strategy"

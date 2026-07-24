from __future__ import annotations

from src.worker.routing import CommercialQueueGate, CommercialQueueThreshold, QueuePressure


def _gate() -> CommercialQueueGate:
    return CommercialQueueGate(
        CommercialQueueThreshold(queue_depth=5)
    )


def test_commercial_gate_stays_closed_under_normal_load():
    gate = _gate()
    assert gate.allow_new(QueuePressure(queue_depth=4)) is False


def test_commercial_gate_opens_at_the_single_queue_threshold():
    gate = _gate()
    assert gate.allow_new(QueuePressure(queue_depth=5)) is True
    assert gate.allow_new(QueuePressure(queue_depth=6)) is True
    assert gate.allow_new(QueuePressure(queue_depth=4)) is False

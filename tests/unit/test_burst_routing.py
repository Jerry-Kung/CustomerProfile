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


def test_parallel_mode_always_allows_commercial_consumption():
    gate = CommercialQueueGate(
        CommercialQueueThreshold(
            queue_depth=100,
            routing_mode="parallel",
            spillover_delay_seconds=600,
        )
    )

    assert gate.static_decision() is True
    assert gate.allow_new(QueuePressure(queue_depth=0)) is True


def test_local_preferred_mode_spills_over_by_oldest_wait_age():
    gate = CommercialQueueGate(
        CommercialQueueThreshold(
            queue_depth=100,
            routing_mode="local_preferred",
            spillover_delay_seconds=30,
        )
    )

    assert gate.static_decision() is None
    assert gate.allow_new(
        QueuePressure(queue_depth=1, oldest_queued_age_seconds=29)
    ) is False
    assert gate.allow_new(
        QueuePressure(queue_depth=1, oldest_queued_age_seconds=30)
    ) is True


def test_local_only_mode_keeps_commercial_gate_closed():
    gate = CommercialQueueGate(
        CommercialQueueThreshold(queue_depth=1, routing_mode="local_only")
    )

    assert gate.static_decision() is False
    assert gate.allow_new(
        QueuePressure(queue_depth=100, oldest_queued_age_seconds=1000)
    ) is False

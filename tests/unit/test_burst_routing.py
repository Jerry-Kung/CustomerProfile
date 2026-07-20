from __future__ import annotations

from src.worker.routing import BurstGate, BurstThresholds, QueuePressure


def _gate() -> BurstGate:
    return BurstGate(
        BurstThresholds(
            enabled=True,
            high_watermark=10,
            low_watermark=3,
            oldest_trigger_seconds=600,
            oldest_recover_seconds=120,
        )
    )


def test_commercial_gate_stays_closed_under_normal_load():
    gate = _gate()
    assert gate.allow_new(QueuePressure(queue_depth=9, oldest_queued_age_seconds=599)) is False


def test_commercial_gate_opens_on_depth_or_wait_age():
    depth_gate = _gate()
    age_gate = _gate()
    assert depth_gate.allow_new(QueuePressure(queue_depth=10, oldest_queued_age_seconds=10)) is True
    assert age_gate.allow_new(QueuePressure(queue_depth=1, oldest_queued_age_seconds=600)) is True


def test_hysteresis_keeps_gate_open_until_both_recovery_conditions_hold():
    gate = _gate()
    assert gate.allow_new(QueuePressure(10, 600)) is True
    assert gate.allow_new(QueuePressure(4, 100)) is True
    assert gate.allow_new(QueuePressure(2, 200)) is True
    assert gate.allow_new(QueuePressure(3, 120)) is False


def test_disabled_gate_never_allows_new_commercial_tasks():
    gate = BurstGate(
        BurstThresholds(
            enabled=False,
            high_watermark=10,
            low_watermark=3,
            oldest_trigger_seconds=600,
            oldest_recover_seconds=120,
        )
    )
    assert gate.allow_new(QueuePressure(100, 3600)) is False

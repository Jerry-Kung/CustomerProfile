from __future__ import annotations

from src.domain.task.state_machine import can_transition


def test_allowed_transitions():
    assert can_transition("queued", "processing")
    assert can_transition("processing", "retry_wait")
    assert can_transition("retry_wait", "processing")
    assert can_transition("retry_wait", "failed")
    assert can_transition("processing", "succeeded")
    assert can_transition("processing", "failed")


def test_blocked_transitions():
    assert not can_transition("queued", "succeeded")
    assert not can_transition("retry_wait", "succeeded")
    assert not can_transition("failed", "processing")
    assert not can_transition("succeeded", "failed")


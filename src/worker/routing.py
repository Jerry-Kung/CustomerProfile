from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueuePressure:
    queue_depth: int


@dataclass(frozen=True)
class CommercialQueueThreshold:
    queue_depth: int


class CommercialQueueGate:
    """Allow normal commercial work once the queue reaches one fixed threshold."""

    def __init__(self, threshold: CommercialQueueThreshold):
        self._threshold = threshold

    def allow_new(self, pressure: QueuePressure) -> bool:
        return pressure.queue_depth >= self._threshold.queue_depth

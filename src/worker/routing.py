from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueuePressure:
    queue_depth: int
    oldest_queued_age_seconds: int | None = None


@dataclass(frozen=True)
class CommercialQueueThreshold:
    queue_depth: int
    routing_mode: str = "local_preferred"
    spillover_delay_seconds: int = 600


class CommercialQueueGate:
    """Decide whether commercial targets may claim ordinary queued work."""

    def __init__(self, threshold: CommercialQueueThreshold):
        self._threshold = threshold

    def static_decision(self) -> bool | None:
        """Return a decision that does not require a queue-pressure query."""
        if self._threshold.routing_mode == "parallel":
            return True
        if self._threshold.routing_mode == "local_only":
            return False
        return None

    def allow_new(self, pressure: QueuePressure) -> bool:
        static_decision = self.static_decision()
        if static_decision is not None:
            return static_decision
        if pressure.queue_depth >= self._threshold.queue_depth:
            return True
        age = pressure.oldest_queued_age_seconds
        return age is not None and age >= self._threshold.spillover_delay_seconds

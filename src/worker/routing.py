from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class QueuePressure:
    queue_depth: int
    oldest_queued_age_seconds: int | None


@dataclass(frozen=True)
class BurstThresholds:
    enabled: bool
    high_watermark: int
    low_watermark: int
    oldest_trigger_seconds: int
    oldest_recover_seconds: int


class BurstGate:
    """Stateful high/low-watermark gate shared by all threads in one worker."""

    def __init__(self, thresholds: BurstThresholds):
        self._thresholds = thresholds
        self._active = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def allow_new(self, pressure: QueuePressure) -> bool:
        with self._lock:
            if not self._thresholds.enabled:
                self._active = False
                return False

            age = pressure.oldest_queued_age_seconds or 0
            if not self._active:
                self._active = (
                    pressure.queue_depth >= self._thresholds.high_watermark
                    or age >= self._thresholds.oldest_trigger_seconds
                )
                return self._active

            recovered = (
                pressure.queue_depth <= self._thresholds.low_watermark
                and age <= self._thresholds.oldest_recover_seconds
            )
            if recovered:
                self._active = False
            return self._active

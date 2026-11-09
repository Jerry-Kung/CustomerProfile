from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class TargetCircuitBreaker:
    """Small per-process circuit breaker for one configured workflow target."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds < 1:
            raise ValueError("cooldown_seconds must be >= 1")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow_unbound_work(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._open_until <= 0:
                return True
            if now < self._open_until:
                return False
            self._open_until = 0.0
            self._consecutive_failures = 0
            return True

    def record_failure(self) -> bool:
        """Record a target-level failure; return True when the circuit opens."""
        with self._lock:
            now = self._clock()
            was_open = self._open_until > now
            self._consecutive_failures += 1
            if self._consecutive_failures < self._failure_threshold:
                return False
            self._open_until = now + self._cooldown_seconds
            return not was_open

    def record_success(self) -> bool:
        """Close/reset the circuit; return True when non-clean state changed."""
        with self._lock:
            changed = self._consecutive_failures > 0 or self._open_until > 0
            self._consecutive_failures = 0
            self._open_until = 0.0
            return changed

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            remaining = max(0.0, self._open_until - self._clock())
            return {
                "target_circuit_state": "open" if remaining > 0 else "closed",
                "target_consecutive_failures": self._consecutive_failures,
                "target_circuit_remaining_seconds": int(math.ceil(remaining)),
            }

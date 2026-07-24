from __future__ import annotations

ALLOWED_TRANSITIONS = {
    "queued": {"processing"},
    "processing": {"retry_wait", "succeeded", "failed"},
    "retry_wait": {"processing", "failed"},
    "succeeded": set(),
    "failed": set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())


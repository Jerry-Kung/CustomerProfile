from __future__ import annotations

from dataclasses import dataclass


THROUGHPUT_FIRST_V1 = "throughput_first_v1"
LOCAL_FIRST_V1 = "local_first_v1"
SINGLE_TARGET_V1 = "single_target_v1"
SUPPORTED_RETRY_POLICIES = {
    THROUGHPUT_FIRST_V1,
    LOCAL_FIRST_V1,
    SINGLE_TARGET_V1,
}


@dataclass(frozen=True)
class AttemptLimits:
    max_total_attempts: int
    max_pool_attempts: int
    max_target_attempts: int


def attempt_limits(
    *,
    policy_version: str,
    execution_pool: str,
    max_total_attempts: int,
) -> AttemptLimits:
    """Return database-enforced budgets for one new remote submission.

    ``throughput_first_v1`` prefers a target that has not handled this
    generation yet, but target uniqueness is deliberately not a hard budget.
    A deployment may have fewer live targets than ``max_total_attempts``; the
    repository then allows a prior target to retry after a short grace period
    so the task always reaches a terminal state instead of waiting forever.
    """
    if policy_version == SINGLE_TARGET_V1:
        return AttemptLimits(
            max_total_attempts=max_total_attempts,
            max_pool_attempts=max_total_attempts,
            max_target_attempts=max_total_attempts,
        )
    if policy_version == LOCAL_FIRST_V1:
        pool_limit = 2 if execution_pool == "local" else 1
        return AttemptLimits(
            max_total_attempts=max_total_attempts,
            max_pool_attempts=pool_limit,
            max_target_attempts=pool_limit,
        )
    return AttemptLimits(
        max_total_attempts=max_total_attempts,
        max_pool_attempts=max_total_attempts,
        max_target_attempts=max_total_attempts,
    )


def next_execution_pool(
    *,
    policy_version: str,
    attempt_count: int,
    local_attempt_count: int,
    commercial_attempt_count: int,
    max_total_attempts: int,
) -> str | None:
    """Choose the pool for the next *new* run after a definite failure."""
    if attempt_count >= max_total_attempts:
        return None
    if policy_version == SINGLE_TARGET_V1:
        return "local" if local_attempt_count else "commercial"
    if policy_version == LOCAL_FIRST_V1:
        if local_attempt_count < 2:
            return "local"
        if commercial_attempt_count < 1:
            return "commercial"
        return None
    return "any"


def retry_policy_for_task(*, default_policy_version: str, task_type: str) -> str:
    """Keep single-workflow task types on their only compatible target."""
    if task_type == "invitation_strategy":
        return SINGLE_TARGET_V1
    return default_policy_version

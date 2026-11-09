from src.domain.task.retry_policy import (
    LOCAL_FIRST_V1,
    SINGLE_TARGET_V1,
    THROUGHPUT_FIRST_V1,
    attempt_limits,
    next_execution_pool,
    retry_policy_for_task,
)


def test_throughput_policy_uses_soft_target_preference_with_one_total_budget():
    limits = attempt_limits(
        policy_version=THROUGHPUT_FIRST_V1,
        execution_pool="commercial",
        max_total_attempts=3,
    )

    assert limits.max_total_attempts == 3
    assert limits.max_pool_attempts == 3
    assert limits.max_target_attempts == 3
    assert (
        next_execution_pool(
            policy_version=THROUGHPUT_FIRST_V1,
            attempt_count=2,
            local_attempt_count=1,
            commercial_attempt_count=1,
            max_total_attempts=3,
        )
        == "any"
    )
    assert (
        next_execution_pool(
            policy_version=THROUGHPUT_FIRST_V1,
            attempt_count=3,
            local_attempt_count=1,
            commercial_attempt_count=2,
            max_total_attempts=3,
        )
        is None
    )


def test_local_first_policy_keeps_two_local_and_one_commercial_budget():
    local_limits = attempt_limits(
        policy_version=LOCAL_FIRST_V1,
        execution_pool="local",
        max_total_attempts=3,
    )
    commercial_limits = attempt_limits(
        policy_version=LOCAL_FIRST_V1,
        execution_pool="commercial",
        max_total_attempts=3,
    )

    assert local_limits.max_pool_attempts == 2
    assert local_limits.max_target_attempts == 2
    assert commercial_limits.max_pool_attempts == 1
    assert commercial_limits.max_target_attempts == 1
    assert (
        next_execution_pool(
            policy_version=LOCAL_FIRST_V1,
            attempt_count=1,
            local_attempt_count=1,
            commercial_attempt_count=0,
            max_total_attempts=3,
        )
        == "local"
    )
    assert (
        next_execution_pool(
            policy_version=LOCAL_FIRST_V1,
            attempt_count=2,
            local_attempt_count=2,
            commercial_attempt_count=0,
            max_total_attempts=3,
        )
        == "commercial"
    )


def test_single_target_policy_reuses_its_only_target_with_a_total_cap():
    limits = attempt_limits(
        policy_version=SINGLE_TARGET_V1,
        execution_pool="local",
        max_total_attempts=3,
    )

    assert limits.max_total_attempts == 3
    assert limits.max_pool_attempts == 3
    assert limits.max_target_attempts == 3
    assert (
        next_execution_pool(
            policy_version=SINGLE_TARGET_V1,
            attempt_count=1,
            local_attempt_count=1,
            commercial_attempt_count=0,
            max_total_attempts=3,
        )
        == "local"
    )
    assert (
        next_execution_pool(
            policy_version=SINGLE_TARGET_V1,
            attempt_count=3,
            local_attempt_count=3,
            commercial_attempt_count=0,
            max_total_attempts=3,
        )
        is None
    )


def test_invitation_strategy_uses_the_single_target_policy():
    assert (
        retry_policy_for_task(
            default_policy_version=THROUGHPUT_FIRST_V1,
            task_type="invitation_strategy",
        )
        == SINGLE_TARGET_V1
    )
    assert (
        retry_policy_for_task(
            default_policy_version=THROUGHPUT_FIRST_V1,
            task_type="customer_profile",
        )
        == THROUGHPUT_FIRST_V1
    )

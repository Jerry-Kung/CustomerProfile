from src.worker.target_health import TargetCircuitBreaker


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_target_circuit_opens_then_allows_work_after_cooldown():
    clock = _Clock()
    circuit = TargetCircuitBreaker(
        failure_threshold=3,
        cooldown_seconds=60,
        clock=clock,
    )

    assert circuit.record_failure() is False
    assert circuit.record_failure() is False
    assert circuit.record_failure() is True
    assert circuit.allow_unbound_work() is False
    assert circuit.snapshot() == {
        "target_circuit_state": "open",
        "target_consecutive_failures": 3,
        "target_circuit_remaining_seconds": 60,
    }

    clock.now += 60
    assert circuit.allow_unbound_work() is True
    assert circuit.snapshot()["target_circuit_state"] == "closed"
    assert circuit.snapshot()["target_consecutive_failures"] == 0


def test_target_success_resets_consecutive_failures():
    circuit = TargetCircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=60,
    )

    assert circuit.record_failure() is False
    assert circuit.record_success() is True
    assert circuit.record_success() is False
    assert circuit.snapshot()["target_consecutive_failures"] == 0

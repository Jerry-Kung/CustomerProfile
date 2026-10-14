from __future__ import annotations

from dataclasses import dataclass
import zlib


@dataclass(frozen=True)
class ProfileRoutingPolicy:
    """First-attempt split; overdue work may spill to the other group."""

    enabled: bool = False
    runner_percent: int = 80
    spillover_seconds: int = 300
    work_envs: tuple[str, ...] = ("production",)

    def __post_init__(self) -> None:
        if not 0 <= self.runner_percent <= 100:
            raise ValueError("PROFILE_RUNNER_TRAFFIC_PERCENT must be between 0 and 100")
        if self.spillover_seconds < 1:
            raise ValueError("PROFILE_ROUTING_SPILLOVER_SECONDS must be >= 1")
        if not self.work_envs or set(self.work_envs) - {"test", "production"}:
            raise ValueError("PROFILE_ROUTING_WORK_ENVS must contain test or production")


def profile_bucket(phone: str, batch_id: str, work_env: str = "production") -> int:
    """Match MySQL CRC32, so retries and every worker agree on the first group."""
    key = f"customer_profile:{work_env}:{phone}:{batch_id}"
    return zlib.crc32(key.encode("utf-8")) % 100

"""LLM 速率限额（RPM / TPM，V0.5.3）。

`LLM_RPM_LIMIT` / `LLM_TPM_LIMIT` 自 V0.2 起就声明在 ``Settings`` 里，**全仓无任何
读取方**——与 ``MAX_ACTIVE_RUNS`` 在 v0.5.1 之前是同一形态的死配置。这个文件的核心
职责就是让「配了必须真的生效」与「没配完全不干扰」两件事都被断言钉住。

三条不可协商的性质：

1. **限额作用在「每次真实出网」那一层。** 若放在 ``llm_call`` 的循环外层，3 次尝试
   只会记 1 个请求，配额被系统性低估 3 倍。
2. **回放不消耗额度。** 回放请求没有真的发出去；让它排队会让全量测试凭空变慢，还会
   掩盖真实限额行为。
3. **多 worker 是显式分配。** LIMIT 是全局额度，每进程用其中一份；不可整除时必须
   启动报错，否则会出现「RPM=60 配 4 个 worker，实际打到 240」的静默超发。

用**假时钟 + 假 sleeper** 测，不真 sleep：RPM 的等待动辄几十秒。
"""

from __future__ import annotations

import pytest

from customer_profile.execution.llm import LlmClient
from customer_profile.execution.quota import (
    LlmQuota,
    TokenBucket,
    estimate_prompt_tokens,
)
from customer_profile.settings import Settings


class FakeClock:
    """可手动推进的单调钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleeper:
    """假装睡觉：记下等待时长并推进时钟，使后续补充立刻可见。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)


def _settings(**overrides) -> Settings:
    """测试用配置。

    **限额必须显式置空。** ``Settings`` 会自动读仓库根目录的 ``.env``，而本机 ``.env``
    里已配了 ``LLM_RPM_LIMIT=10000`` / ``LLM_TPM_LIMIT=500000``——不显式覆盖的话，
    那些「未配置时不生效」的断言会被真实配置污染，测的就不是本文件要证的性质了。
    """
    base = {
        "llm_base_url": "http://127.0.0.1:1/v1",
        "llm_api_key": "k",
        "llm_model": "m",
        "database_url": "sqlite:///./data/x.db",
        "llm_rpm_limit": None,
        "llm_tpm_limit": None,
        "llm_rpm_burst": None,
        "llm_tpm_burst": None,
        "worker_replicas": 1,
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------- 令牌桶


async def test_bucket_allows_up_to_capacity_without_waiting():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(rate_per_s=1.0, capacity=3.0, clock=clock, sleeper=sleeper)

    for _ in range(3):
        await bucket.take(1.0)
    assert sleeper.waits == [], "容量内不该有任何等待"


async def test_bucket_waits_exactly_as_long_as_needed():
    """第 4 个请求要等 1 个令牌补回来——等 1 秒，不是等一整分钟。"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(rate_per_s=1.0, capacity=3.0, clock=clock, sleeper=sleeper)

    for _ in range(3):
        await bucket.take(1.0)
    await bucket.take(1.0)

    assert len(sleeper.waits) == 1
    assert sleeper.waits[0] == pytest.approx(1.0, abs=0.01)


async def test_bucket_refills_capped_at_capacity():
    """长时间空闲不会攒出超过容量的额度，否则一次突发就能打穿限额。"""
    clock = FakeClock()
    bucket = TokenBucket(rate_per_s=1.0, capacity=3.0, clock=clock)
    clock.advance(10_000)
    assert bucket.available() == pytest.approx(3.0)


async def test_bucket_rejects_a_single_request_larger_than_capacity():
    """单次需求超过桶容量：显式报错，而不是死等。这是配置问题。"""
    bucket = TokenBucket(rate_per_s=1.0, capacity=5.0)
    with pytest.raises(ValueError):
        await bucket.take(10.0)


async def test_bucket_refund_is_capped_at_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_s=1.0, capacity=3.0, clock=clock)
    bucket.refund(100.0)
    assert bucket.available() == pytest.approx(3.0)


# ---------------------------------------------------------------- 未配置即不干扰


async def test_quota_is_disabled_when_limits_are_unset():
    """两个限额都未配置时完全不生效——这是「死配置」的反面。

    若这里失败（例如 acquire 仍会等待），说明限额在没配置时也在干扰调用，
    那会让所有既有测试与单机部署凭空变慢。
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    quota = LlmQuota(_settings(), clock=clock, sleeper=sleeper)

    assert quota.enabled is False
    for _ in range(100):
        await quota.acquire({"messages": [{"role": "user", "content": "x"}]})
    assert sleeper.waits == []


async def test_only_rpm_configured_does_not_enable_tpm():
    """配一个维度不该顺带激活另一个。"""
    quota = LlmQuota(_settings(llm_rpm_limit=60))
    assert quota.rpm_enabled is True
    assert quota.tpm_enabled is False


# ---------------------------------------------------------------- RPM


async def test_rpm_waits_after_the_bucket_is_drained():
    """RPM=60 即 1 个/秒；桶满时连放 60 个，第 61 个开始等。"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    quota = LlmQuota(
        _settings(llm_rpm_limit=60, llm_rpm_burst=60), clock=clock, sleeper=sleeper
    )

    for _ in range(60):
        await quota.acquire({"messages": [{"role": "user", "content": "x"}]})
    assert sleeper.waits == [], "容量内不该等待"

    await quota.acquire({"messages": [{"role": "user", "content": "x"}]})
    assert len(sleeper.waits) == 1
    assert sleeper.waits[0] == pytest.approx(1.0, abs=0.05)


async def test_rpm_burst_defaults_to_the_limit():
    quota = LlmQuota(_settings(llm_rpm_limit=30))
    assert quota.snapshot()["rpm_capacity"] == pytest.approx(30.0)


# ---------------------------------------------------------------- TPM


async def test_tpm_is_reserved_before_the_request_and_settled_after():
    """预扣按 prompt 估算，随后按真实 usage 校正。

    预扣是必需的（不预扣就无从在出网前限制），校正也是必需的（不校正则估算偏差
    会持续累积，长会话下额度越用越偏）。
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    quota = LlmQuota(
        _settings(llm_tpm_limit=100_000, llm_tpm_burst=100_000),
        clock=clock,
        sleeper=sleeper,
    )
    payload = {"messages": [{"role": "user", "content": "x" * 2000}]}
    est = estimate_prompt_tokens(payload)
    assert est > 0

    reserved = await quota.acquire(payload)
    assert reserved == pytest.approx(float(est))

    class Usage:
        total_tokens = est + 500

    await quota.settle(reserved, Usage())
    # 真实用量比预扣多 500，应被追扣：可用额度相应减少。
    assert quota.snapshot()["tpm_available"] <= 100_000 - (est + 500) + 1e-6


async def test_tpm_refunds_when_the_estimate_was_too_high():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    quota = LlmQuota(
        _settings(llm_tpm_limit=10_000, llm_tpm_burst=10_000),
        clock=clock,
        sleeper=sleeper,
    )
    payload = {"messages": [{"role": "user", "content": "x" * 4000}]}
    reserved = await quota.acquire(payload)

    class Usage:
        total_tokens = 10

    await quota.settle(reserved, Usage())
    # 预扣远高于真实用量，多扣的部分应被退还。
    assert quota.snapshot()["tpm_available"] == pytest.approx(10_000 - 10, abs=1.0)


async def test_tpm_does_not_refund_when_usage_is_unknown():
    """``usage`` 缺失时**不校正**：未知不能当成 0 退还，那会凭空放大额度。"""
    clock = FakeClock()
    quota = LlmQuota(
        _settings(llm_tpm_limit=10_000, llm_tpm_burst=10_000), clock=clock
    )
    payload = {"messages": [{"role": "user", "content": "x" * 4000}]}
    reserved = await quota.acquire(payload)
    before = quota.snapshot()["tpm_available"]

    await quota.settle(reserved, None)
    assert quota.snapshot()["tpm_available"] == pytest.approx(before, abs=1e-6)


async def test_estimate_counts_images():
    """图片输入要计入估算：vision 节点的 prompt 里正文很短、图片成本很高。"""
    text_only = estimate_prompt_tokens(
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    )
    with_image = estimate_prompt_tokens(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    ],
                }
            ]
        }
    )
    assert with_image > text_only


# ---------------------------------------------------------------- 多 worker 显式分配


async def test_per_process_limits_split_across_replicas():
    """限额按 worker 数等分：全局限额 60 配 2 个 worker，每进程 30。"""
    settings = _settings(llm_rpm_limit=60, worker_replicas=2)
    assert settings.per_process_rpm_limit == 30
    assert settings.per_process_tpm_limit is None


def test_limits_must_divide_evenly_across_workers():
    """不可整除必须**启动即报错**。

    否则 RPM=60 配 4 个 worker 会静默打到 240——限额悄悄失效且毫无提示，
    正是本版本要消灭的那类问题。
    """
    with pytest.raises(Exception):
        _settings(llm_rpm_limit=60, worker_replicas=7)


def test_single_worker_is_identity():
    settings = _settings(llm_rpm_limit=60)
    assert settings.per_process_rpm_limit == 60


# ---------------------------------------------------------------- 接线点


async def test_replay_does_not_consume_quota():
    """回放模式不消耗额度。

    回放请求没有真的发出去，让它排队不但让全量测试凭空变慢，还会掩盖真实限额行为。
    """
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    quota = LlmQuota(
        _settings(llm_rpm_limit=1, llm_rpm_burst=1), clock=clock, sleeper=sleeper
    )

    class FakeReplay:
        strict = True

        def next_llm(self, payload):
            return {"text": "hello", "usage": {}}

    client = LlmClient(_settings(llm_rpm_limit=1), replay=FakeReplay(), quota=quota)

    # 回放路径提前返回，压根不该走到 quota.acquire
    for _ in range(5):
        attempt = await client._single_attempt({"model": "m", "messages": []}, 1)
        assert attempt.replayed is True
    assert sleeper.waits == [], "回放消耗了配额"


async def test_quota_rejects_oversized_request_instead_of_hanging():
    """单次请求超过 TPM 桶容量时显式报错，而不是无限等待。"""
    clock = FakeClock()
    quota = LlmQuota(
        _settings(llm_tpm_limit=100, llm_tpm_burst=100), clock=clock
    )
    huge = {"messages": [{"role": "user", "content": "x" * 1_000_000}]}
    with pytest.raises(ValueError):
        await quota.acquire(huge)

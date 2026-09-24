"""模型侧速率限制：RPM / TPM（V0.5.3）。

规划 §3 V0.5 交付物 3 要求「全局请求限额（覆盖子流程内部调用）+ 模型侧 RPM/TPM 约束」。
并发限制（``MAX_CONCURRENT_REQUESTS``）在本项目早已存在，但它回答的是「同时打出去几个」，
不回答「一分钟总共几个」——两者是不同维度，因此**并存而不是合并**。

三处刻意的取舍：

1. **令牌桶，不是固定窗口。** 固定窗口在窗口边界上会瞬时放行 2× 配额；本项目一次运行
   有 32 次 LLM 调用、历时二十余分钟，恰好会撞在边界上。令牌桶同样是几十行代码，给出
   的是「长期速率 ≤ limit、瞬时不超过 burst」这一明确语义。
2. **接线点在「一次真实出网」那一层，不在调用点。** 调用点之外还有重试循环：放在循环
   外层会让 3 次尝试只计 1 个请求，配额被系统性低估 3 倍。
3. **回放不消耗令牌。** ``REPLAY_MODE=fixture`` 时请求没有真的发出去，让它排队只会让
   全量测试凭空变慢，且会掩盖真实的限额行为。

多 worker 的语义是**显式分配**：本模块的限额器是进程内的，``LLM_RPM_LIMIT`` 的含义是
**每个进程**的限额，全局限额 = 每进程限额 × ``WORKER_REPLICAS``。跨进程共享配额需要
中间件，与「不引入未实际需要的重型框架」冲突（规划 §5 约束 7），故不做。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucket:
    """令牌桶。按 ``rate``（每秒补充量）补充，容量上限为 ``capacity``。

    ``clock`` 与 ``sleep`` 可注入，使测试能用假时钟推进而不是真的等待——RPM 限额的
    等待动辄几十秒，靠真实 ``sleep`` 测会让测试慢到不可接受。
    """

    def __init__(
        self,
        *,
        rate_per_s: float,
        capacity: float,
        clock: Clock = time.monotonic,
        sleeper: Sleeper | None = None,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError(f"补充速率必须为正，实际为 {rate_per_s}")
        if capacity <= 0:
            raise ValueError(f"桶容量必须为正，实际为 {capacity}")
        self._rate = rate_per_s
        self._capacity = capacity
        self._tokens = capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def rate_per_s(self) -> float:
        return self._rate

    def available(self) -> float:
        """当前可用令牌数（按调用时刻折算已补充的量）。"""
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        return min(self._capacity, self._tokens + elapsed * self._rate)

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    async def take(self, amount: float = 1.0) -> None:
        """取走 ``amount`` 个令牌。不足时**等待**到够为止，不抛错。

        不抛错是刻意的：调用方此刻唯一能做的就是等（限额是外部事实，不是本次调用的
        错误）。抛错会把它变成一个「节点失败」，而实际上什么事都没发生。
        """
        if amount <= 0:
            return
        if amount > self._capacity:
            # 单次需求超过桶容量：永远等不到。这是配置问题，必须显式报出而不是死等。
            raise ValueError(
                f"单次需要 {amount} 个令牌，超过桶容量 {self._capacity}；"
                "请提高对应的 burst 配置"
            )
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                deficit = amount - self._tokens
                wait = deficit / self._rate
            await self._sleep(wait)

    def refund(self, amount: float) -> None:
        """退还令牌（用于 token 预扣后按真实用量校正）。不超过桶容量。"""
        if amount <= 0:
            return
        self._refill_locked()
        self._tokens = min(self._capacity, self._tokens + amount)


def estimate_prompt_tokens(payload: Mapping[str, Any]) -> int:
    """粗估一次请求的 prompt token 数，用于**预扣**。

    必须在拿到响应之前就扣，否则限额形同虚设；而真实用量只有响应里才有，因此这里
    保守高估，再由 :meth:`LlmQuota.settle` 按真实 ``usage`` 校正。

    估算口径：按字符数的一半计。中文字符约 1 token/字，英文约 4 字符/token，取 2
    字符/token 对中文偏保守、对英文偏宽松——两者都只是预扣，校正会把偏差抹平。
    图片按每张固定成本计（视觉 token 与分辨率相关，无法精确预知）。
    """
    chars = 0
    images = 0
    messages = payload.get("messages") or []
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "text":
                    chars += len(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    images += 1
    return max(1, chars // 2) + images * 1000


class LlmQuota:
    """LLM 的 RPM 与 TPM 限额。两个维度各一个令牌桶，互不挤占。

    任一限额未配置（``None``）时该维度**完全不生效**——这与
    :class:`~customer_profile.settings.Settings` 里「空串按未设置处理」的口径一致，
    也是「死配置」的反面：配了就必须真的起作用，没配就不该干扰。
    """

    def __init__(
        self,
        settings: Any,
        *,
        clock: Clock = time.monotonic,
        sleeper: Sleeper | None = None,
    ) -> None:
        rpm = getattr(settings, "llm_rpm_limit", None)
        tpm = getattr(settings, "llm_tpm_limit", None)
        burst_rpm = getattr(settings, "llm_rpm_burst", None) or rpm
        burst_tpm = getattr(settings, "llm_tpm_burst", None) or tpm

        self._rpm = (
            TokenBucket(
                rate_per_s=rpm / 60.0,
                capacity=float(burst_rpm),
                clock=clock,
                sleeper=sleeper,
            )
            if rpm
            else None
        )
        self._tpm = (
            TokenBucket(
                rate_per_s=tpm / 60.0,
                capacity=float(burst_tpm),
                clock=clock,
                sleeper=sleeper,
            )
            if tpm
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._rpm is not None or self._tpm is not None

    @property
    def rpm_enabled(self) -> bool:
        return self._rpm is not None

    @property
    def tpm_enabled(self) -> bool:
        return self._tpm is not None

    async def acquire(self, payload: Mapping[str, Any]) -> float:
        """出网前取额度。返回**预扣的 token 数**，供 :meth:`settle` 校正。

        顺序是「先速率、后并发」：速率等待可能长达数十秒，若先占用并发名额等待，
        会把其它本来可以出网的请求一起堵住。
        """
        if self._rpm is not None:
            await self._rpm.take(1.0)
        if self._tpm is None:
            return 0.0
        reserved = float(estimate_prompt_tokens(payload))
        await self._tpm.take(reserved)
        return reserved

    async def settle(self, reserved: float, usage: Any | None) -> None:
        """按真实用量校正 TPM 预扣。

        ``usage`` 为 ``None``（请求根本没发出去，或响应没有 usage）时不校正——
        「未知」不能当成 0 去退还，那会凭空放大可用额度。
        """
        if self._tpm is None:
            return
        total = getattr(usage, "total_tokens", None) if usage is not None else None
        if total is None:
            return
        delta = float(total) - float(reserved)
        if delta > 0:
            await self._tpm.take(delta)
        elif delta < 0:
            self._tpm.refund(-delta)

    def snapshot(self) -> dict[str, Any]:
        """当前额度概览，供 ``GET /runtime`` 展示。不含任何凭据。"""
        return {
            "rpm_enabled": self.rpm_enabled,
            "tpm_enabled": self.tpm_enabled,
            "rpm_available": self._rpm.available() if self._rpm else None,
            "tpm_available": self._tpm.available() if self._tpm else None,
            "rpm_capacity": self._rpm.capacity if self._rpm else None,
            "tpm_capacity": self._tpm.capacity if self._tpm else None,
        }

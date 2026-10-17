"""令牌桶限速器。"""

from __future__ import annotations

import pytest

from deepseek_search.ratelimit import TokenBucket


class ManualClock:
    """配合 sleep 手动推进的时钟。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_burst_requests_do_not_wait():
    clock = ManualClock()
    bucket = TokenBucket(2.0, burst=2, clock=clock, sleep=clock.sleep)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == 0.0
    assert clock.sleeps == []


async def test_exhausted_tokens_wait_for_refill():
    clock = ManualClock()
    bucket = TokenBucket(2.0, burst=1, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    waited = await bucket.acquire()
    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]


async def test_refilled_tokens_do_not_wait():
    clock = ManualClock()
    bucket = TokenBucket(2.0, burst=1, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    clock.now += 1.0  # 补充 2 个令牌，上限 1
    assert await bucket.acquire() == 0.0


def test_update_limits_clamps_tokens():
    clock = ManualClock()
    bucket = TokenBucket(4.0, burst=4, clock=clock, sleep=clock.sleep)
    bucket.update_limits(1.0, 2)
    snapshot = bucket.snapshot()
    assert snapshot.qps == 1.0
    assert snapshot.burst == 2
    assert snapshot.tokens <= 2


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
    with pytest.raises(ValueError):
        TokenBucket(1, 0)


async def test_update_limits_rejects_invalid_values():
    bucket = TokenBucket(1.0, 1)
    with pytest.raises(ValueError):
        bucket.update_limits(0, 1)
    with pytest.raises(ValueError):
        bucket.update_limits(1, 0)

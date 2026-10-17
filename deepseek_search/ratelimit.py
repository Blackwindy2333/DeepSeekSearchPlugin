"""本地令牌桶限速。

官方限制是「每账号 5 QPS」，且与 API Key 数量无关。多个用户同时触发搜索时，
单靠服务端 429 才发现超限会带来额外延迟与失败重试，因此在客户端再压一层：

- 默认 4 QPS（留 20% 余量），突发 2；
- 限速是**等待**而不是**丢弃**：拿不到令牌就 ``await``，最坏等待时间由 ``qps`` 决定；
- 令牌桶按时间戳惰性补充，无后台任务，``on_unload`` 无需清理。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(slots=True)
class RateLimitSnapshot:
    """限速器当前状态快照（用于首页卡片与日志）。"""

    qps: float
    burst: int
    tokens: float
    waiting: int


class TokenBucket:
    """异步令牌桶限速器。"""

    def __init__(
        self,
        qps: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """初始化限速器。

        Args:
            qps: 每秒补充的令牌数，必须大于 0。
            burst: 桶容量（允许的瞬时突发数），至少为 1。
            clock: 单调时钟，测试可注入假时钟。
            sleep: 异步等待函数，测试可注入假实现。

        Raises:
            ValueError: ``qps`` 或 ``burst`` 不合法。
        """

        if qps <= 0:
            raise ValueError("qps 必须大于 0")
        if burst < 1:
            raise ValueError("burst 至少为 1")

        self._qps = float(qps)
        self._burst = int(burst)
        self._tokens = float(burst)
        self._updated_at = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._waiting = 0

    @property
    def qps(self) -> float:
        """当前每秒补充速率。"""

        return self._qps

    @property
    def burst(self) -> int:
        """当前桶容量。"""

        return self._burst

    def update_limits(self, qps: float, burst: int) -> None:
        """热更新速率与容量（配置热重载时调用）。

        Args:
            qps: 新的每秒补充速率。
            burst: 新的桶容量。

        Raises:
            ValueError: 参数不合法。
        """

        if qps <= 0:
            raise ValueError("qps 必须大于 0")
        if burst < 1:
            raise ValueError("burst 至少为 1")
        self._qps = float(qps)
        self._burst = int(burst)
        self._tokens = min(self._tokens, float(burst))

    def _refill(self) -> None:
        """按流逝时间补充令牌。"""

        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        if elapsed <= 0:
            return
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._qps)

    async def acquire(self) -> float:
        """获取一个令牌，必要时等待。

        Returns:
            float: 实际等待的秒数（0 表示未等待）。
        """

        self._waiting += 1
        try:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return 0.0

                deficit = 1.0 - self._tokens
                wait_seconds = deficit / self._qps
                self._tokens = 0.0
                await self._sleep(wait_seconds)
                self._refill()
                self._tokens = max(0.0, self._tokens)
                self._tokens -= 1.0
                return wait_seconds
        finally:
            self._waiting = max(0, self._waiting - 1)

    def snapshot(self) -> RateLimitSnapshot:
        """返回当前状态快照。"""

        self._refill()
        return RateLimitSnapshot(
            qps=self._qps,
            burst=self._burst,
            tokens=round(self._tokens, 3),
            waiting=self._waiting,
        )


__all__ = ["RateLimitSnapshot", "TokenBucket"]

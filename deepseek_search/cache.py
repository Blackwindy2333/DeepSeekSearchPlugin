"""带 TTL 的 LRU 缓存（进程内、单实例）。

为什么自己写而不是引入 ``cachetools``：需求只有「TTL + 容量上限 + 命中率统计」，
标准库 ``OrderedDict`` 十行就能实现，少一个依赖就少一处供应链与版本冲突风险。

并发模型：所有方法都是同步的（微秒级），在事件循环里直接调用即可；
``max_entries`` 与 TTL 支持热更新，缓存内容不随配置重载清空。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

V = TypeVar("V")


@dataclass(slots=True)
class CacheSnapshot:
    """缓存状态快照。"""

    entries: int
    max_entries: int
    hits: int
    misses: int
    evictions: int

    @property
    def hit_rate(self) -> float:
        """命中率（无请求时为 0）。"""

        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


class TTLCache(Generic[V]):
    """TTL + LRU 缓存。"""

    def __init__(
        self,
        max_entries: int = 256,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化缓存。

        Args:
            max_entries: 条数上限，至少为 1。
            clock: 单调时钟，测试可注入假时钟。

        Raises:
            ValueError: ``max_entries`` 小于 1。
        """

        if max_entries < 1:
            raise ValueError("max_entries 至少为 1")
        self._max_entries = int(max_entries)
        self._clock = clock
        self._store: "OrderedDict[Hashable, tuple[float, V]]" = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """当前缓存条数（含可能已过期但未清理的条目）。"""

        return len(self._store)

    def get(self, key: Hashable) -> V | None:
        """读取缓存；过期条目视为未命中。

        Args:
            key: 缓存键。

        Returns:
            V | None: 命中时返回值，否则返回 None。
        """

        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if expires_at <= self._clock():
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: Hashable, value: V, ttl_seconds: float) -> None:
        """写入缓存。

        Args:
            key: 缓存键。
            value: 缓存值。
            ttl_seconds: 有效期（秒），小于等于 0 表示不缓存。
        """

        if ttl_seconds <= 0:
            return
        with self._lock:
            self._store[key] = (self._clock() + ttl_seconds, value)
            self._store.move_to_end(key)
            self._purge_expired_locked()
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
                self._evictions += 1

    def clear(self) -> None:
        """清空缓存（不重置命中统计）。"""

        with self._lock:
            self._store.clear()

    def resize(self, max_entries: int) -> None:
        """热更新容量上限。

        Args:
            max_entries: 新的条数上限。

        Raises:
            ValueError: 小于 1。
        """

        if max_entries < 1:
            raise ValueError("max_entries 至少为 1")
        with self._lock:
            self._max_entries = int(max_entries)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
                self._evictions += 1

    def _purge_expired_locked(self) -> None:
        """清理已过期条目（调用方需持锁）。"""

        now = self._clock()
        expired = [key for key, (expires_at, _) in self._store.items() if expires_at <= now]
        for key in expired:
            self._store.pop(key, None)

    def snapshot(self) -> CacheSnapshot:
        """返回状态快照。"""

        with self._lock:
            self._purge_expired_locked()
            return CacheSnapshot(
                entries=len(self._store),
                max_entries=self._max_entries,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )

    def stats(self) -> dict[str, Any]:
        """返回可 JSON 序列化的统计字典。"""

        snapshot = self.snapshot()
        return {
            "entries": snapshot.entries,
            "max_entries": snapshot.max_entries,
            "hits": snapshot.hits,
            "misses": snapshot.misses,
            "evictions": snapshot.evictions,
            "hit_rate": snapshot.hit_rate,
        }


__all__ = ["CacheSnapshot", "TTLCache"]

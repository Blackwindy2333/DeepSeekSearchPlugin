"""TTL + LRU 缓存。"""

from __future__ import annotations

import pytest

from deepseek_search.cache import TTLCache


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_cache_hit_and_miss_counters():
    clock = FakeClock()
    cache = TTLCache(4, clock=clock)
    cache.set("k", "v", 10)
    assert cache.get("k") == "v"
    assert cache.get("missing") is None
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = TTLCache(4, clock=clock)
    cache.set("k", "v", ttl_seconds=5)
    clock.advance(4.9)
    assert cache.get("k") == "v"
    clock.advance(0.2)
    assert cache.get("k") is None
    assert cache.snapshot().entries == 0


def test_non_positive_ttl_is_not_cached():
    cache = TTLCache(4)
    cache.set("k", "v", 0)
    assert cache.get("k") is None


def test_lru_eviction_respects_max_entries():
    cache = TTLCache(2)
    cache.set("a", 1, 60)
    cache.set("b", 2, 60)
    assert cache.get("a") == 1  # a 变为最近使用
    cache.set("c", 3, 60)
    stats = cache.snapshot()
    assert stats.entries == 2
    assert stats.evictions == 1
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_resize_drops_oldest_entries():
    cache = TTLCache(4)
    for index in range(4):
        cache.set(index, index, 60)
    cache.resize(2)
    stats = cache.snapshot()
    assert stats.entries == 2
    assert stats.max_entries == 2
    assert cache.get(0) is None
    assert cache.get(3) == 3


def test_clear_keeps_counters():
    cache = TTLCache(4)
    cache.set("a", 1, 60)
    cache.get("a")
    cache.clear()
    assert cache.snapshot().entries == 0
    assert cache.stats()["hits"] == 1


def test_invalid_max_entries_is_rejected():
    with pytest.raises(ValueError):
        TTLCache(0)
    cache = TTLCache(2)
    with pytest.raises(ValueError):
        cache.resize(0)

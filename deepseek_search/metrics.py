"""运行统计。

只做进程内累加，不做持久化：数据用于首页卡片、``get_stats`` API 与周期日志，
重启归零是可接受的语义（历史趋势交给日志分析）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Metrics:
    """插件运行统计。"""

    started_at: float = field(default_factory=time.time)
    search_requests: int = 0
    search_success: int = 0
    search_empty: int = 0
    search_failed: int = 0
    fetch_requests: int = 0
    fetch_success: int = 0
    fetch_failed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    disk_hits: int = 0
    disk_saves: int = 0
    disk_errors: int = 0
    retries: int = 0
    ratelimit_waits: int = 0
    ratelimit_wait_seconds: float = 0.0
    total_elapsed_ms: float = 0.0
    last_error: str = ""
    last_query: str = ""
    last_success_at: float | None = None

    def record_search(self, *, results: int, elapsed_ms: float, cached: bool) -> None:
        """记录一次成功的搜索。

        Args:
            results: 返回结果条数。
            elapsed_ms: 耗时（毫秒）。
            cached: 是否命中缓存。
        """

        self.search_requests += 1
        self.search_success += 1
        self.total_elapsed_ms += elapsed_ms
        if results == 0:
            self.search_empty += 1
        if cached:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        self.last_success_at = time.time()

    def record_search_failure(self, message: str) -> None:
        """记录一次搜索失败。"""

        self.search_requests += 1
        self.search_failed += 1
        self.cache_misses += 1
        self.last_error = message

    def record_fetch(self, *, elapsed_ms: float, cached: bool, chars: int) -> None:
        """记录一次成功的抓取。

        Args:
            elapsed_ms: 耗时（毫秒）。
            cached: 是否命中缓存。
            chars: 返回正文字符数。
        """

        self.fetch_requests += 1
        self.fetch_success += 1
        self.total_elapsed_ms += elapsed_ms
        if cached:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        self.last_success_at = time.time()

    def record_fetch_failure(self, message: str) -> None:
        """记录一次抓取失败。"""

        self.fetch_requests += 1
        self.fetch_failed += 1
        self.cache_misses += 1
        self.last_error = message

    def record_retry(self) -> None:
        """记录一次重试。"""

        self.retries += 1

    def record_disk_hit(self) -> None:
        """记录一次命中网页正文磁盘缓存。"""

        self.disk_hits += 1

    def record_disk_save(self) -> None:
        """记录一次网页正文写入磁盘缓存。"""

        self.disk_saves += 1

    def record_disk_error(self) -> None:
        """记录一次磁盘缓存读写失败（已在调用处降级，不影响主流程）。"""

        self.disk_errors += 1

    def record_ratelimit_wait(self, seconds: float) -> None:
        """记录一次因本地限速产生的等待。"""

        if seconds <= 0:
            return
        self.ratelimit_waits += 1
        self.ratelimit_wait_seconds += seconds

    @property
    def uptime_seconds(self) -> float:
        """运行时长（秒）。"""

        return max(0.0, time.time() - self.started_at)

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率。"""

        total = self.cache_hits + self.cache_misses
        return round(self.cache_hits / total, 4) if total else 0.0

    def snapshot(self) -> dict[str, Any]:
        """返回可 JSON 序列化的统计快照。"""

        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "search": {
                "requests": self.search_requests,
                "success": self.search_success,
                "empty": self.search_empty,
                "failed": self.search_failed,
            },
            "fetch": {
                "requests": self.fetch_requests,
                "success": self.fetch_success,
                "failed": self.fetch_failed,
            },
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": self.cache_hit_rate,
            },
            "pages": {
                "disk_hits": self.disk_hits,
                "disk_saves": self.disk_saves,
                "disk_errors": self.disk_errors,
            },
            "retries": self.retries,
            "ratelimit": {
                "waits": self.ratelimit_waits,
                "wait_seconds": round(self.ratelimit_wait_seconds, 2),
            },
            "last_error": self.last_error,
            "last_query": self.last_query,
        }


__all__ = ["Metrics"]

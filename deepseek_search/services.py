"""业务编排层。

这一层把「配置 + 限速 + 缓存 + HTTP 客户端 + 统计 + 格式化」串起来，对外只暴露
``search`` / ``fetch`` / ``snapshot`` 三个动作，组件层（Tool/Command/API/HomeCard）
保持极薄，后续要加「多引擎聚合」「结果落库」之类的能力，只需要改这里。

关键策略：

- **配置热重载**：``apply_config`` 就地更新限速与缓存参数；只有 API 连接参数变化时才重建客户端；
- **缓存键**包含所有影响结果的参数，命中缓存不消耗限速令牌、不计费；
- **异常不外抛**：所有失败都转成 ``Outcome``（带中文 ``content``），保证给 LLM 的永远是可读文本。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .cache import TTLCache
from .client import DeepSeekClient
from .config import DeepSeekSearchConfig
from .errors import ConfigurationError, DeepSeekSearchError
from .formatting import format_error, format_fetch_result, format_search_result
from .logging_utils import get_child
from .metrics import Metrics
from .models import FetchResult, SearchResult
from .ratelimit import TokenBucket
from .settings import Settings

if TYPE_CHECKING:
    from .pages import PageRecord, PageStore

#: 日志中 query 的最大展示长度（避免刷屏与泄露长文本）
LOG_QUERY_LIMIT = 120


def _shorten(text: str, limit: int = LOG_QUERY_LIMIT) -> str:
    """把文本压缩到单行、限长，用于日志。"""

    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


@dataclass(slots=True)
class SearchOutcome:
    """一次搜索的对外结果。"""

    success: bool
    content: str
    query: str = ""
    result: SearchResult | None = None
    cached: bool = False
    elapsed_ms: float = 0.0
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成 Tool / API 返回值（``content`` 是给 LLM 的正文）。"""

        payload: dict[str, Any] = {
            "success": self.success,
            "content": self.content,
            "query": self.query,
            "cached": self.cached,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }
        if self.result is not None:
            payload["result_count"] = len(self.result.items)
        if self.error:
            payload["error"] = self.error
        if self.extra:
            payload.update(self.extra)
        return payload


@dataclass(slots=True)
class FetchOutcome:
    """一次抓取的对外结果。"""

    success: bool
    content: str
    url: str = ""
    result: FetchResult | None = None
    cached: bool = False
    from_disk: bool = False
    elapsed_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成 Tool / API 返回值。"""

        payload: dict[str, Any] = {
            "success": self.success,
            "content": self.content,
            "url": self.url,
            "cached": self.cached,
            "from_disk": self.from_disk,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }
        if self.result is not None:
            payload["title"] = self.result.title
            payload["chars"] = len(self.result.content)
            payload["truncated"] = self.result.truncated
            payload["offset"] = self.result.offset
            payload["total_chars"] = self.result.total_chars
            payload["has_more"] = self.result.has_more
        if self.error:
            payload["error"] = self.error
        return payload


class DeepSeekSearchService:
    """搜索 / 抓取业务编排。"""

    def __init__(
        self,
        config: DeepSeekSearchConfig,
        *,
        logger: logging.Logger | None = None,
        env: Settings | None = None,
        metrics: Metrics | None = None,
        client_factory: Callable[[dict[str, Any], logging.Logger], DeepSeekClient] | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        """创建服务实例。

        Args:
            config: 插件配置实例。
            logger: 日志器（通常是基日志器的子日志器）。
            env: 环境变量设置，缺省用 ``dsp.settings.settings``。
            metrics: 统计对象，缺省新建。
            client_factory: 客户端工厂（测试可注入假客户端）。
            data_dir: 插件数据目录；提供后才会启用网页正文磁盘缓存。
        """

        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._env = env or Settings.from_env()
        self._metrics = metrics or Metrics()
        self._client_factory = client_factory or self._default_client_factory
        self._client: DeepSeekClient | None = None
        self._client_signature: tuple[Any, ...] = ()
        self._closed = False
        self._data_dir = data_dir
        self._page_store: PageStore | None = None

        self._cache: TTLCache[Any] = TTLCache(config.cache.max_entries)
        self._bucket: TokenBucket | None = self._make_bucket(config)

    # ── 构造辅助 ────────────────────────────────────────────

    @staticmethod
    def _make_bucket(config: DeepSeekSearchConfig) -> TokenBucket | None:
        """按配置创建令牌桶；未启用时返回 None。"""

        if not config.ratelimit.enabled:
            return None
        return TokenBucket(float(config.ratelimit.qps), int(config.ratelimit.burst))

    def _effective(self, config: DeepSeekSearchConfig | None = None) -> dict[str, Any]:
        """合并配置与环境变量，得到本次实际生效的 API 参数。"""

        cfg = config or self._config
        return {
            "api_key": self._env.api_key or cfg.api.api_key,
            "base_url": self._env.base_url or cfg.api.base_url,
            "proxy": self._env.proxy or cfg.api.proxy,
            "timeout_seconds": float(self._env.http_timeout or cfg.api.timeout_seconds),
            "connect_timeout_seconds": float(cfg.api.connect_timeout_seconds),
            "max_retries": int(cfg.api.max_retries),
            "retry_backoff_seconds": float(cfg.api.retry_backoff_seconds),
            "retry_max_backoff_seconds": float(cfg.api.retry_max_backoff_seconds),
            "log_requests": bool(cfg.logging.log_requests),
            "preview_chars": int(cfg.logging.log_response_preview_chars),
        }

    def _signature(self, config: DeepSeekSearchConfig | None = None) -> tuple[Any, ...]:
        """API 连接参数签名，用于判断是否需要重建客户端。"""

        effective = self._effective(config)
        return tuple(effective[key] for key in sorted(effective))

    def _default_client_factory(self, effective: dict[str, Any], logger: logging.Logger) -> DeepSeekClient:
        """默认客户端工厂。"""

        return DeepSeekClient(
            effective["api_key"],
            base_url=effective["base_url"],
            proxy=effective["proxy"],
            timeout_seconds=effective["timeout_seconds"],
            connect_timeout_seconds=effective["connect_timeout_seconds"],
            max_retries=effective["max_retries"],
            retry_backoff_seconds=effective["retry_backoff_seconds"],
            retry_max_backoff_seconds=effective["retry_max_backoff_seconds"],
            logger=logger,
            bucket=self._bucket,
            metrics=self._metrics,
            log_requests=effective["log_requests"],
            preview_chars=effective["preview_chars"],
        )

    def _ensure_page_store(self) -> PageStore | None:
        """按需创建 / 更新网页正文磁盘缓存。

        ``pages.py`` 采用函数内延迟导入：即使部署包缺少该模块，插件其余功能也能
        正常加载；只有真正需要读写磁盘缓存时才会 import。

        Returns:
            PageStore | None: 磁盘缓存实例；未启用或未配置数据目录时为 None。
        """

        cfg = self._config
        if not cfg.pages.enabled or self._data_dir in (None, ""):
            return None
        from .pages import PageStore

        if self._page_store is None:
            self._page_store = PageStore(
                Path(self._data_dir) / "pages",
                ttl_seconds=cfg.pages.ttl_seconds,
                max_entries=cfg.pages.max_entries,
                max_total_bytes=cfg.pages.max_total_bytes,
                logger=get_child(self._logger, "pages"),
            )
        else:
            self._page_store.update_settings(
                ttl_seconds=cfg.pages.ttl_seconds,
                max_entries=cfg.pages.max_entries,
                max_total_bytes=cfg.pages.max_total_bytes,
            )
        return self._page_store

    # ── 生命周期 ────────────────────────────────────────────

    async def apply_config(self, config: DeepSeekSearchConfig) -> dict[str, Any]:
        """应用新配置（热重载入口）。

        Args:
            config: 新的配置实例。

        Returns:
            dict[str, Any]: 变更摘要，供插件写日志。
        """

        old = self._config
        changes: dict[str, Any] = {}
        old_bucket = self._bucket
        if old.ratelimit != config.ratelimit:
            changes["ratelimit"] = {
                "enabled": config.ratelimit.enabled,
                "qps": config.ratelimit.qps,
                "burst": config.ratelimit.burst,
            }
            new_bucket = self._make_bucket(config)
            if new_bucket is None:
                self._bucket = None
            elif self._bucket is None:
                self._bucket = new_bucket
            else:
                self._bucket.update_limits(float(config.ratelimit.qps), int(config.ratelimit.burst))
        if old.cache != config.cache:
            self._cache.resize(int(config.cache.max_entries))
            changes["cache"] = {
                "enabled": config.cache.enabled,
                "max_entries": config.cache.max_entries,
                "search_ttl_seconds": config.cache.search_ttl_seconds,
                "fetch_ttl_seconds": config.cache.fetch_ttl_seconds,
            }
        pages_changed = old.pages != config.pages
        if pages_changed:
            changes["pages"] = "updated"

        self._config = config
        if pages_changed:
            # 磁盘缓存已存在时就地更新 TTL/容量；enabled 关闭时 store 仍保留但 fetch 不再使用。
            self._ensure_page_store()
        new_signature = self._signature(config)
        # 客户端持有创建时传入的令牌桶引用：限速开关切换会替换桶对象，
        # 此时必须重建客户端，否则旧客户端会继续按旧桶限速（或继续完全不限速）。
        bucket_swapped = self._bucket is not old_bucket
        if (new_signature != self._client_signature or bucket_swapped) and self._client is not None:
            changes["client"] = "rebuilt"
            await self.aclose_client()
        return changes

    async def aclose_client(self) -> None:
        """关闭当前 HTTP 客户端（幂等）。"""

        client = self._client
        self._client = None
        self._client_signature = ()
        if client is not None:
            await client.aclose()

    async def aclose(self) -> None:
        """释放资源（插件卸载时调用）。"""

        await self.aclose_client()
        self._closed = True

    # ── 对外动作 ────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """是否具备可用的 API Key。"""

        return bool(self._effective()["api_key"])

    def _ensure_client(self) -> DeepSeekClient:
        """按需创建/复用客户端。

        Returns:
            DeepSeekClient: 可用客户端。

        Raises:
            DeepSeekSearchError: 配置缺失（缺少 Key 等）。
        """

        if self._closed:
            raise ConfigurationError("搜索服务已关闭", user_message="插件正在重载，请稍后再试")
        signature = self._signature()
        if self._client is not None and signature == self._client_signature:
            return self._client
        effective = self._effective()
        client_logger = self._logger.getChild("client")
        client = self._client_factory(effective, client_logger)
        self._client = client
        self._client_signature = signature
        self._logger.debug("HTTP 客户端就绪 base_url=%s key=%s", client.base_url, client.masked_api_key)
        return client

    async def search(
        self,
        query: str,
        *,
        count: int | None = None,
        need_content: bool | None = None,
        ori_question: str = "",
        localization: str | None = None,
    ) -> SearchOutcome:
        """执行一次联网搜索（含缓存与限速）。

        Args:
            query: 关键词/搜索语法。
            count: 结果条数，None 用配置默认值。
            need_content: 是否附带正文，None 用配置默认值。
            ori_question: 原始问题，用于重排。
            localization: 本地化倾向，None 用配置默认值。

        Returns:
            SearchOutcome: 统一结果对象。
        """

        cfg = self._config
        started = time.monotonic()
        rid = uuid4().hex[:8]
        clean_query = str(query or "").strip()
        if not clean_query:
            return SearchOutcome(
                success=False,
                content="请提供搜索关键词",
                query="",
                error="query 为空",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        safe_count = int(count) if count else int(cfg.search.default_count)
        safe_count = max(1, min(50, safe_count))
        safe_need_content = bool(cfg.search.need_content if need_content is None else need_content)
        safe_localization = cfg.search.localization if localization is None else str(localization)
        ori = str(ori_question or "")[: max(0, int(cfg.search.max_ori_question_chars))]

        if cfg.logging.log_requests:
            self._logger.info(
                "search.request rid=%s query=%r count=%d need_content=%s",
                rid,
                _shorten(clean_query),
                safe_count,
                safe_need_content,
            )

        cache_key = ("search", clean_query, safe_count, safe_need_content, ori, safe_localization)
        if cfg.cache.enabled:
            cached = self._cache.get(cache_key)
            if isinstance(cached, SearchResult):
                elapsed_ms = (time.monotonic() - started) * 1000
                self._metrics.record_search(results=len(cached.items), elapsed_ms=elapsed_ms, cached=True)
                self._metrics.last_query = clean_query
                if cfg.logging.log_requests:
                    self._logger.info(
                        "search.cache_hit rid=%s results=%d elapsed_ms=%.1f",
                        rid,
                        len(cached.items),
                        elapsed_ms,
                    )
                return SearchOutcome(
                    success=True,
                    content=self._format_search(cached),
                    query=clean_query,
                    result=cached,
                    cached=True,
                    elapsed_ms=elapsed_ms,
                )

        try:
            client = self._ensure_client()
            result = await client.search(
                clean_query,
                count=safe_count,
                need_content=safe_need_content,
                ori_question=ori,
                localization=safe_localization,
            )
        except DeepSeekSearchError as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._metrics.record_search_failure(exc.user_message)
            self._logger.warning(
                "search.failed rid=%s error_class=%s status=%s message=%s elapsed_ms=%.1f",
                rid,
                type(exc).__name__,
                exc.status_code,
                _shorten(str(exc)),
                elapsed_ms,
            )
            return SearchOutcome(
                success=False,
                content=f"搜索失败：{format_error(exc)}",
                query=clean_query,
                error=exc.user_message,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，避免异常穿透到 Host
            elapsed_ms = (time.monotonic() - started) * 1000
            self._logger.exception("search.error rid=%s unexpected_error=%s", rid, exc)
            self._metrics.record_search_failure("未预期错误")
            return SearchOutcome(
                success=False,
                content=f"搜索失败：{format_error(exc)}",
                query=clean_query,
                error=str(exc),
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - started) * 1000
        if cfg.cache.enabled:
            self._cache.set(cache_key, result, float(cfg.cache.search_ttl_seconds))
        self._metrics.record_search(results=len(result.items), elapsed_ms=elapsed_ms, cached=False)
        self._metrics.last_query = clean_query
        if cfg.logging.log_requests:
            self._logger.info(
                "search.success rid=%s results=%d cache=miss elapsed_ms=%.1f",
                rid,
                len(result.items),
                elapsed_ms,
            )
        return SearchOutcome(
            success=True,
            content=self._format_search(result),
            query=clean_query,
            result=result,
            cached=False,
            elapsed_ms=elapsed_ms,
        )

    async def fetch(
        self,
        url: str,
        *,
        max_chars: int | None = None,
        offset: int = 0,
    ) -> FetchOutcome:
        """抓取单个网页（含缓存与限速），支持从正文任意位置分段读取。

        Args:
            url: 目标链接。
            max_chars: 返回正文的最大字符数，None 用配置默认值。
            offset: 从正文第几个字符开始读取（0 表示开头），用于长文续读。

        Returns:
            FetchOutcome: 统一结果对象。
        """

        cfg = self._config
        started = time.monotonic()
        rid = uuid4().hex[:8]
        target = str(url or "").strip()
        limit = int(max_chars) if max_chars else int(cfg.fetch.max_content_chars)
        limit = max(200, min(30000, limit))
        start = max(0, int(offset))

        if cfg.logging.log_requests:
            self._logger.info(
                "fetch.request rid=%s url=%s max_chars=%d offset=%d",
                rid,
                _shorten(target),
                limit,
                start,
            )

        cache_key = ("fetch", target)
        full: FetchResult | None = None
        memory_hit = False
        if cfg.cache.enabled:
            cached = self._cache.get(cache_key)
            if isinstance(cached, FetchResult):
                full = cached
                memory_hit = True

        from_disk = False
        if full is None:
            # 内存缓存未命中（或已关闭）时先看磁盘：命中则完全不调用 API。
            full = await self._load_page_from_disk(target, rid)
            from_disk = full is not None

        if full is not None:
            elapsed_ms = (time.monotonic() - started) * 1000
            if memory_hit and cfg.logging.log_requests:
                self._logger.info(
                    "fetch.cache_hit rid=%s chars=%d elapsed_ms=%.1f",
                    rid,
                    len(full.content),
                    elapsed_ms,
                )
            # 内存/磁盘命中都没有调用 API，统一按缓存命中计数；磁盘命中另记 pages.disk_hits。
            self._metrics.record_fetch(elapsed_ms=elapsed_ms, cached=True, chars=len(full.content))
            if from_disk:
                self._metrics.record_disk_hit()
                self._logger.info("fetch.disk_hit rid=%s chars=%d", rid, len(full.content))
            return self._build_fetch_outcome(
                full,
                url=target,
                limit=limit,
                offset=start,
                cached=True,
                from_disk=from_disk,
                elapsed_ms=elapsed_ms,
                rid=rid,
            )

        try:
            client = self._ensure_client()
            result = await client.fetch(target)
        except DeepSeekSearchError as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._metrics.record_fetch_failure(exc.user_message)
            self._logger.warning(
                "fetch.failed rid=%s error_class=%s status=%s message=%s elapsed_ms=%.1f",
                rid,
                type(exc).__name__,
                exc.status_code,
                _shorten(str(exc)),
                elapsed_ms,
            )
            return FetchOutcome(
                success=False,
                content=f"抓取失败：{format_error(exc)}",
                url=target,
                error=exc.user_message,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.monotonic() - started) * 1000
            self._logger.exception("fetch.error rid=%s unexpected_error=%s", rid, exc)
            self._metrics.record_fetch_failure("未预期错误")
            return FetchOutcome(
                success=False,
                content=f"抓取失败：{format_error(exc)}",
                url=target,
                error=str(exc),
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - started) * 1000
        truncated = len(result.content) > limit
        full = FetchResult(url=result.url, title=result.title, content=result.content)
        if cfg.cache.enabled:
            stored = FetchResult(
                url=result.url,
                title=result.title,
                content=result.content,
                truncated=truncated,
            )
            self._cache.set(cache_key, stored, float(cfg.cache.fetch_ttl_seconds))
        await self._save_page_to_disk(target, full, rid)
        self._metrics.record_fetch(elapsed_ms=elapsed_ms, cached=False, chars=len(result.content))
        if cfg.logging.log_requests:
            self._logger.info(
                "fetch.success rid=%s chars=%d truncated=%s cache=miss elapsed_ms=%.1f",
                rid,
                len(result.content),
                truncated,
                elapsed_ms,
            )
        return self._build_fetch_outcome(
            full,
            url=target,
            limit=limit,
            offset=start,
            cached=False,
            from_disk=False,
            elapsed_ms=elapsed_ms,
            rid=rid,
        )

    # ── 内部：网页正文磁盘缓存 ──────────────────────────────

    async def _load_page_from_disk(self, target: str, rid: str) -> FetchResult | None:
        """从磁盘缓存读取完整正文；未启用、未命中或读取失败时返回 None。"""

        store = self._ensure_page_store()
        if store is None:
            return None
        try:
            record: PageRecord | None = await asyncio.to_thread(store.get, target)
        except Exception as exc:  # noqa: BLE001 - 磁盘缓存读失败只降级为回源，不影响抓取
            self._metrics.record_disk_error()
            self._logger.warning("fetch.disk_error rid=%s action=read error=%s", rid, _shorten(str(exc)))
            return None
        if record is None:
            return None
        return FetchResult(
            url=record.url or target,
            title=record.title,
            content=record.content,
            total_chars=len(record.content),
        )

    async def _save_page_to_disk(self, target: str, full: FetchResult, rid: str) -> None:
        """把完整正文写入磁盘缓存；失败只记 warning，不影响本次抓取结果。"""

        store = self._ensure_page_store()
        if store is None:
            return
        try:
            # PageRecord 与 PageStore 同源，延迟导入保证缺页时只有磁盘缓存降级。
            from .pages import PageRecord

            record = PageRecord(url=target, title=full.title, content=full.content, fetched_at=time.time())
            await asyncio.to_thread(store.put, record)
        except Exception as exc:  # noqa: BLE001 - 磁盘缓存写失败只降级为「不缓存」
            self._metrics.record_disk_error()
            self._logger.warning("fetch.disk_error rid=%s action=write error=%s", rid, _shorten(str(exc)))
            return
        self._metrics.record_disk_save()

    def _build_fetch_outcome(
        self,
        full: FetchResult,
        *,
        url: str,
        limit: int,
        offset: int,
        cached: bool,
        from_disk: bool,
        elapsed_ms: float,
        rid: str,
    ) -> FetchOutcome:
        """按 ``offset`` / ``limit`` 截取正文窗口并构造对外结果。"""

        cfg = self._config
        total = len(full.content)
        if offset > 0 and offset >= total:
            return FetchOutcome(
                success=True,
                content=f"（已到正文末尾，共 {total} 字）",
                url=url,
                result=FetchResult(
                    url=url,
                    title=full.title,
                    content="",
                    truncated=False,
                    offset=total,
                    total_chars=total,
                ),
                cached=cached,
                from_disk=from_disk,
                elapsed_ms=elapsed_ms,
            )
        window = full.content[offset : offset + limit]
        view = FetchResult(
            url=url,
            title=full.title,
            content=window,
            truncated=offset + len(window) < total,
            offset=offset,
            total_chars=total,
        )
        formatted = format_fetch_result(
            view,
            max_content_chars=limit,
            include_title=bool(cfg.fetch.include_title),
        )
        if cfg.logging.log_requests:
            self._logger.info(
                "fetch.window rid=%s offset=%d total=%d returned=%d",
                rid,
                offset,
                total,
                len(window),
            )
        return FetchOutcome(
            success=True,
            content=formatted,
            url=url,
            result=view,
            cached=cached,
            from_disk=from_disk,
            elapsed_ms=elapsed_ms,
        )

    # ── 状态 ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """返回运行状态快照（API / 首页卡片 / 日志用）。"""

        cfg = self._config
        return {
            "ready": self.ready,
            "api": {
                "base_url": self._effective()["base_url"],
                "configured": self.ready,
                "source": "env" if self._env.api_key else ("config" if cfg.api.api_key else "none"),
            },
            "metrics": self._metrics.snapshot(),
            "cache": self._cache.stats(),
            "pages": self._page_snapshot(),
            "ratelimit": None if self._bucket is None else {
                "enabled": True,
                "qps": self._bucket.qps,
                "burst": self._bucket.burst,
                "tokens": self._bucket.snapshot().tokens,
            },
            "config": {
                "search_default_count": cfg.search.default_count,
                "need_content": cfg.search.need_content,
                "fetch_max_content_chars": cfg.fetch.max_content_chars,
                "cache_enabled": cfg.cache.enabled,
                "commands_enabled": cfg.commands.enabled,
            },
        }

    def _page_snapshot(self) -> dict[str, Any]:
        """磁盘缓存状态：命中/写入/失败统计 + 容量占用（未启用时容量为 0）。"""

        cfg = self._config
        store = self._page_store
        stats: dict[str, Any] = store.stats() if store is not None else {}
        return {
            "enabled": bool(store is not None and cfg.pages.enabled),
            "entries": int(stats.get("entries", 0)),
            "total_bytes": int(stats.get("total_bytes", 0)),
            "max_entries": int(stats.get("max_entries", cfg.pages.max_entries)),
            "max_total_bytes": int(stats.get("max_total_bytes", cfg.pages.max_total_bytes)),
            "disk_hits": self._metrics.disk_hits,
            "disk_saves": self._metrics.disk_saves,
            "disk_errors": self._metrics.disk_errors,
        }

    # ── 内部：格式化 ────────────────────────────────────────

    def _format_search(self, result: SearchResult) -> str:
        """按当前配置格式化搜索结果。"""

        cfg = self._config
        return format_search_result(
            result,
            max_results=max(1, min(int(cfg.search.max_results_in_text), len(result.items) or 1)),
            snippet_max_chars=int(cfg.search.snippet_max_chars),
            summary_max_chars=int(cfg.search.summary_max_chars),
            content_max_chars=int(cfg.search.content_max_chars),
            show_authority_hint=bool(cfg.search.show_authority_hint),
        )


__all__ = ["DeepSeekSearchService", "FetchOutcome", "SearchOutcome"]

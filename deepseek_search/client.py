"""DeepSeek Search / Fetch HTTP 客户端。

职责边界：

- 只负责「一次 API 调用」的正确性：参数校验、鉴权头、超时、限速、重试、错误归一、响应解析；
- **不做缓存、不做格式化、不碰聊天消息**——这些在 ``services`` / ``formatting`` 层；
- 配置变更时整体重建实例（``services.rebuild``），避免「运行中改 Key」的状态残留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from . import errors
from .http import MAX_URL_LENGTH, validate_url
from .models import FetchResult, SearchResult
from .ratelimit import TokenBucket
from .settings import mask_secret

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    import httpx

    from .metrics import Metrics

#: DeepSeek 搜索接口路径
SEARCH_PATH = "/v1/tools/search"
#: DeepSeek 抓取接口路径
FETCH_PATH = "/v1/tools/fetch"
#: 官方 query 长度限制（Unicode 字符）
MAX_QUERY_CHARS = 128
#: 官方 count 取值范围
MIN_COUNT, MAX_COUNT = 1, 50
#: 无搜索结果时的固定提示
EMPTY_RESULT_HINT = "没有找到相关结果"


def _import_httpx() -> Any:
    """延迟导入 httpx，缺失时给出可读的配置错误。

    Returns:
        Any: ``httpx`` 模块。

    Raises:
        errors.ConfigurationError: 未安装 httpx。
    """

    try:
        import httpx as module
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise errors.ConfigurationError(
            "缺少依赖 httpx，请重新安装插件依赖",
            user_message="插件依赖 httpx 未安装，请联系管理员重装插件",
        ) from exc
    return module


def _parse_retry_after(headers: Any) -> float | None:
    """解析 ``Retry-After`` 响应头（只支持秒数形式）。

    Args:
        headers: 响应头映射。

    Returns:
        float | None: 等待秒数；无法解析返回 None。
    """

    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:  # pragma: no cover - headers 不是映射时忽略
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def _extract_error_message(payload: Any, fallback: str) -> tuple[str, str]:
    """从错误响应中提取 ``message`` 与 ``code``。

    Args:
        payload: 已解析的 JSON（可能不是对象）。
        fallback: 兜底文本。

    Returns:
        tuple[str, str]: ``(message, code)``。
    """

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or "").strip()
            return message or fallback, code
    return fallback, ""


class DeepSeekClient:
    """DeepSeek 搜索/抓取异步客户端。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        proxy: str = "",
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = 5.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
        retry_max_backoff_seconds: float = 8.0,
        logger: logging.Logger | None = None,
        bucket: TokenBucket | None = None,
        metrics: "Metrics | None" = None,
        transport: Any = None,
        sleep: Any = None,
        log_requests: bool = True,
        preview_chars: int = 200,
    ) -> None:
        """创建客户端。

        Args:
            api_key: DeepSeek API Key，必须非空。
            base_url: API 基础地址。
            proxy: HTTP 代理，留空直连。
            timeout_seconds: 单次请求总超时。
            connect_timeout_seconds: 连接超时。
            max_retries: 可重试错误的最大重试次数。
            retry_backoff_seconds: 首次重试等待秒数。
            retry_max_backoff_seconds: 单次重试等待上限。
            logger: 日志器，缺省用模块日志器。
            bucket: 限速令牌桶，None 表示不限速。
            metrics: 统计对象，用于累加重试/限速等待。
            transport: 注入的 httpx transport（测试用 ``httpx.MockTransport``）。
            sleep: 可注入的异步 sleep（测试用）。
            log_requests: 是否记录请求日志。
            preview_chars: 响应预览字符数，0 表示不记录。

        Raises:
            errors.ConfigurationError: 缺少 API Key 或 base_url 非法。
        """

        key = str(api_key or "").strip()
        if not key:
            raise errors.ConfigurationError("api_key 为空")
        normalized_base = str(base_url or "").strip().rstrip("/")
        if not normalized_base.startswith(("http://", "https://")):
            raise errors.ConfigurationError(
                f"base_url 非法: {normalized_base!r}",
                user_message="DeepSeek API 地址配置不正确，应以 http/https 开头",
            )

        self._api_key = key
        self._base_url = normalized_base
        self._max_retries = max(0, int(max_retries))
        self._backoff = max(0.1, float(retry_backoff_seconds))
        self._max_backoff = max(self._backoff, float(retry_max_backoff_seconds))
        self._bucket = bucket
        self._metrics = metrics
        self._logger = logger or logging.getLogger(__name__)
        self._sleep = sleep or asyncio.sleep
        self._log_requests = bool(log_requests)
        self._preview_chars = max(0, int(preview_chars))

        httpx_module = _import_httpx()
        timeout = httpx_module.Timeout(float(timeout_seconds), connect=float(connect_timeout_seconds))
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "headers": {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        }
        if proxy:
            kwargs["proxy"] = str(proxy).strip()
        if transport is not None:
            kwargs["transport"] = transport
        self._httpx = httpx_module
        self._client = httpx_module.AsyncClient(**kwargs)
        self._closed = False

    # ── 只读属性 ────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """API 基础地址。"""

        return self._base_url

    @property
    def masked_api_key(self) -> str:
        """脱敏后的 API Key（可安全写日志）。"""

        return mask_secret(self._api_key)

    @property
    def search_url(self) -> str:
        """搜索接口完整地址。"""

        return f"{self._base_url}{SEARCH_PATH}"

    @property
    def fetch_url(self) -> str:
        """抓取接口完整地址。"""

        return f"{self._base_url}{FETCH_PATH}"

    # ── 公开接口 ────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        count: int = 10,
        need_content: bool = False,
        ori_question: str | None = None,
        localization: str = "",
    ) -> SearchResult:
        """调用联网搜索接口。

        Args:
            query: 搜索关键词/语法，按 Unicode 字符截断到 128 以内。
            count: 结果条数，限制在 1–50。
            need_content: 是否附带网页正文。
            ori_question: 原始问题，用于重排参考。
            localization: 本地化倾向，留空不发送。

        Returns:
            SearchResult: 结构化结果集（可能为空）。

        Raises:
            errors.InvalidRequestError: 关键词为空或超长。
            errors.DeepSeekSearchError: 其分类错误见 ``errors`` 模块。
            errors.ResponseParseError: 响应无法解析。
        """

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise errors.InvalidRequestError("查询为空", user_message="请提供搜索关键词")
        if len(normalized_query) > MAX_QUERY_CHARS:
            self._logger.warning(
                "query 超长已截断 action=search chars=%d limit=%d",
                len(normalized_query),
                MAX_QUERY_CHARS,
            )
            normalized_query = normalized_query[:MAX_QUERY_CHARS]

        safe_count = min(MAX_COUNT, max(MIN_COUNT, int(count or 10)))
        payload: dict[str, Any] = {
            "query": normalized_query,
            "count": safe_count,
            "need_content": bool(need_content),
        }
        if ori_question:
            payload["ori_question"] = str(ori_question)[:MAX_QUERY_CHARS]
        if localization:
            payload["localization"] = str(localization)

        body = await self._request(SEARCH_PATH, payload, action="search")
        try:
            result = SearchResult.from_payload(normalized_query, body, requested_count=safe_count)
        except (TypeError, ValueError) as exc:
            raise errors.ResponseParseError(f"搜索响应解析失败: {exc}") from exc
        if self._log_requests:
            self._logger.debug(
                "search parsed action=search results=%d requested=%d",
                len(result.items),
                safe_count,
            )
        return result

    async def fetch(self, url: str) -> FetchResult:
        """调用网页抓取接口。

        Args:
            url: 目标链接，先做本地校验（协议/主机/长度）。

        Returns:
            FetchResult: 抓取结果（标题 + Markdown 正文）。

        Raises:
            errors.UnsupportedUrlError: 链接未通过本地校验。
            errors.DeepSeekSearchError: 其它 API 错误。
            errors.ResponseParseError: 响应无法解析。
        """

        if len(str(url or "")) > MAX_URL_LENGTH:
            raise errors.UnsupportedUrlError("链接过长", user_message=f"链接长度超过 {MAX_URL_LENGTH} 字符，无法抓取")
        candidate = validate_url(url)
        body = await self._request(FETCH_PATH, {"url": candidate}, action="fetch")
        try:
            result = FetchResult.from_payload(candidate, body)
        except (TypeError, ValueError) as exc:
            raise errors.ResponseParseError(f"抓取响应解析失败: {exc}") from exc
        if self._log_requests:
            self._logger.debug("fetch parsed action=fetch chars=%d", len(result.content))
        return result

    async def aclose(self) -> None:
        """关闭底层连接池（幂等）。"""

        if self._closed:
            return
        self._closed = True
        try:
            await self._client.aclose()
        except Exception as exc:  # pragma: no cover - 关闭失败不应影响卸载
            self._logger.debug("关闭 HTTP 客户端时出现异常（已忽略）: %s", exc)

    async def __aenter__(self) -> "DeepSeekClient":
        """支持 ``async with``。"""

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """退出上下文时关闭连接池。"""

        await self.aclose()

    # ── 内部实现 ────────────────────────────────────────────

    def _next_backoff(self, attempt: int, *, retry_after: float | None = None) -> float:
        """计算第 ``attempt`` 次重试的等待时间（指数退避 + 抖动）。"""

        if retry_after is not None:
            return min(self._max_backoff, max(0.0, retry_after))
        base = min(self._max_backoff, self._backoff * (2 ** max(0, attempt - 1)))
        return min(self._max_backoff, base * (0.5 + random.random() * 0.5))

    async def _await_rate_limit(self) -> None:
        """按需等待本地令牌桶。"""

        if self._bucket is None:
            return
        waited = await self._bucket.acquire()
        if waited > 0:
            self._logger.debug("本地限速等待 action=ratelimit wait_ms=%.1f", waited * 1000)
            if self._metrics is not None:
                self._metrics.record_ratelimit_wait(waited)

    def _log_preview(self, action: str, body: Any) -> None:
        """记录响应预览（截断、脱敏）。"""

        if not self._log_requests or self._preview_chars <= 0:
            return
        try:
            preview = json.dumps(body, ensure_ascii=False)[: self._preview_chars]
        except (TypeError, ValueError):  # pragma: no cover - 极端情况
            preview = str(body)[: self._preview_chars]
        self._logger.debug("响应预览 action=%s preview=%s", action, preview)

    async def _request(self, path: str, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        """发送请求并处理限速/重试/错误归一。

        Args:
            path: 接口路径。
            payload: 请求体。
            action: 日志与统计用的动作名（``search`` / ``fetch``）。

        Returns:
            dict[str, Any]: 解析后的响应体。

        Raises:
            errors.DeepSeekSearchError: 归一后的业务异常。
        """

        last_error: errors.DeepSeekSearchError | None = None
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            await self._await_rate_limit()
            started = time.monotonic()
            if self._log_requests:
                self._logger.info(
                    "请求开始 action=%s attempt=%d/%d payload=%s",
                    action,
                    attempt,
                    attempts,
                    self._preview_payload(payload),
                )
            try:
                response = await self._client.post(f"{self._base_url}{path}", json=payload)
            except self._httpx.TimeoutException as exc:
                last_error = errors.RequestTimeoutError(f"{action} 请求超时: {exc}")
            except self._httpx.HTTPError as exc:
                last_error = errors.NetworkError(f"{action} 网络异常: {exc}")
            else:
                elapsed_ms = (time.monotonic() - started) * 1000
                status = int(getattr(response, "status_code", 0) or 0)
                if status == 200:
                    body = self._parse_body(response, action=action)
                    self._log_preview(action, body)
                    if self._log_requests:
                        self._logger.info(
                            "请求成功 action=%s status=%d elapsed_ms=%.1f",
                            action,
                            status,
                            elapsed_ms,
                        )
                    return body
                last_error = self._error_from_response(response, action=action)
                if self._log_requests:
                    self._logger.warning(
                        "请求失败 action=%s status=%d attempt=%d/%d elapsed_ms=%.1f error_class=%s",
                        action,
                        status,
                        attempt,
                        attempts,
                        elapsed_ms,
                        type(last_error).__name__,
                    )

            if not last_error.retryable or attempt >= attempts:
                raise last_error
            wait = self._next_backoff(attempt, retry_after=last_error.retry_after)
            self._logger.warning(
                "准备重试 action=%s attempt=%d/%d wait_s=%.2f reason=%s",
                action,
                attempt,
                attempts,
                wait,
                type(last_error).__name__,
            )
            if self._metrics is not None:
                self._metrics.record_retry()
            await self._sleep(wait)

        raise last_error or errors.ServerError(f"{action} 请求失败")

    def _preview_payload(self, payload: dict[str, Any]) -> str:
        """把请求体转成可安全记录的预览串。"""

        if not self._log_requests:
            return ""
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover
            text = str(payload)
        limit = max(80, self._preview_chars)
        return text[:limit]

    def _parse_body(self, response: Any, *, action: str) -> dict[str, Any]:
        """解析 200 响应体。

        Args:
            response: httpx 响应对象。
            action: 动作名。

        Returns:
            dict[str, Any]: JSON 对象。

        Raises:
            errors.ResponseParseError: 非 JSON 或不是对象。
        """

        try:
            body = response.json()
        except Exception as exc:  # json 解码异常类型随版本变化，统一兜底
            raise errors.ResponseParseError(f"{action} 响应不是合法 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise errors.ResponseParseError(f"{action} 响应不是 JSON 对象")
        return body

    def _error_from_response(self, response: Any, *, action: str) -> errors.DeepSeekSearchError:
        """把非 200 响应转换成异常。"""

        status = int(getattr(response, "status_code", 0) or 0)
        raw_text = str(getattr(response, "text", "") or "")
        message, code = "", ""
        try:
            payload = response.json()
        except Exception:
            payload = None
        message, code = _extract_error_message(payload, raw_text[:200])
        retry_after = _parse_retry_after(getattr(response, "headers", {}))
        error = errors.error_from_status(status, message, retry_after=retry_after, code=code)
        self._logger.debug(
            "错误响应 action=%s status=%d code=%s message=%s",
            action,
            status,
            code,
            message[:200],
        )
        return error


__all__ = ["DeepSeekClient", "FETCH_PATH", "MAX_QUERY_CHARS", "SEARCH_PATH"]

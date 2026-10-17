"""异常体系。

设计原则：

1. **一次归一**：HTTP 状态码、网络异常、响应解析失败，都在客户端层归一成这里的异常；
2. **文案集中**：``user_message`` 是给用户/LLM 看的中文说明，不要在业务层重复拼接；
3. **可判定重试**：``retryable`` 明确告诉上层要不要重试，避免每个调用点各写一套判断。
"""

from __future__ import annotations


class DeepSeekSearchError(Exception):
    """插件异常基类。"""

    #: 默认的中文用户可读文案
    default_user_message = "搜索服务暂时不可用，请稍后再试"
    #: 是否值得重试（退避重试只针对这些异常）
    retryable = False
    #: 对应的 HTTP 状态码（网络异常等为 None）
    status_code: int | None = None

    def __init__(
        self,
        message: str = "",
        *,
        user_message: str = "",
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
        code: str = "",
    ) -> None:
        """创建异常。

        Args:
            message: 面向开发者的诊断信息（可进日志）。
            user_message: 面向用户/LLM 的中文说明，留空时用 ``default_user_message``。
            status_code: 对应 HTTP 状态码。
            retryable: 是否可重试，留空时用类属性。
            retry_after: 服务端建议的等待秒数（来自 Retry-After）。
            code: 服务端错误码（``error.code``）。
        """

        super().__init__(message or user_message or self.default_user_message)
        self.user_message = user_message or self.default_user_message
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after = retry_after
        self.code = code


class ConfigurationError(DeepSeekSearchError):
    """配置缺失或非法（例如没有填 API Key）。"""

    default_user_message = "尚未配置 DeepSeek API Key，请先在插件配置中填写 api.api_key"


class InvalidRequestError(DeepSeekSearchError):
    """请求参数不合法（HTTP 400），修正参数后才能重试。"""

    default_user_message = "搜索请求参数不合法，请检查关键词或链接"


class AuthenticationError(DeepSeekSearchError):
    """API Key 无效（HTTP 401）。"""

    default_user_message = "DeepSeek API Key 无效或已失效，请检查配置"


class InsufficientBalanceError(DeepSeekSearchError):
    """账户余额不足（HTTP 402）。"""

    default_user_message = "DeepSeek 账户余额不足，请充值后重试"


class ContentForbiddenError(DeepSeekSearchError):
    """目标页面被安全策略拒绝（HTTP 403，仅 fetch）。"""

    default_user_message = "该网页不允许抓取（被安全策略拒绝），可以换一个来源"


class ContentNotFoundError(DeepSeekSearchError):
    """抓不到可用内容（HTTP 404，仅 fetch）。"""

    default_user_message = "没有抓取到可读内容，请确认链接是否正确或换一个链接"


class RequestTimeoutError(DeepSeekSearchError):
    """读取请求超时（HTTP 408）。"""

    default_user_message = "搜索请求超时，正在重试或稍后再试"
    retryable = True


class PayloadTooLargeError(DeepSeekSearchError):
    """请求体过大（HTTP 413，仅 fetch）。"""

    default_user_message = "目标页面体积过大，无法抓取"


class RateLimitedError(DeepSeekSearchError):
    """触发限速（HTTP 429）。"""

    default_user_message = "搜索请求过于频繁，已被限速，请稍后再试"
    retryable = True


class ServerError(DeepSeekSearchError):
    """服务端错误（HTTP 5xx）。"""

    default_user_message = "DeepSeek 搜索服务暂时不可用，请稍后再试"
    retryable = True


class NetworkError(DeepSeekSearchError):
    """网络层异常（DNS、连接、读超时等）。"""

    default_user_message = "连接 DeepSeek 服务失败，请检查网络或代理配置"
    retryable = True


class ResponseParseError(DeepSeekSearchError):
    """响应结构不符合预期。"""

    default_user_message = "搜索服务返回了无法解析的内容"


class UnsupportedUrlError(DeepSeekSearchError):
    """URL 未通过本地校验（协议、主机名等）。"""

    default_user_message = "只支持 http/https 链接，且不能是内网地址"


#: HTTP 状态码 → 异常类型
HTTP_STATUS_ERRORS: dict[int, type[DeepSeekSearchError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    402: InsufficientBalanceError,
    403: ContentForbiddenError,
    404: ContentNotFoundError,
    408: RequestTimeoutError,
    413: PayloadTooLargeError,
    429: RateLimitedError,
    500: ServerError,
    502: ServerError,
    503: ServerError,
    504: ServerError,
}


def error_from_status(status_code: int, message: str = "", *, retry_after: float | None = None, code: str = "") -> DeepSeekSearchError:
    """按 HTTP 状态码构造对应异常。

    Args:
        status_code: HTTP 状态码。
        message: 服务端错误信息（会进日志）。
        retry_after: 建议等待秒数。
        code: 服务端错误码。

    Returns:
        DeepSeekSearchError: 对应的异常实例。
    """

    error_cls = HTTP_STATUS_ERRORS.get(status_code)
    if error_cls is None:
        error_cls = ServerError if status_code >= 500 else InvalidRequestError
    detail = f"HTTP {status_code}"
    if message:
        detail = f"{detail}: {message}"
    return error_cls(
        detail,
        status_code=status_code,
        retry_after=retry_after,
        code=code,
    )


__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "ContentForbiddenError",
    "ContentNotFoundError",
    "DeepSeekSearchError",
    "InsufficientBalanceError",
    "InvalidRequestError",
    "NetworkError",
    "PayloadTooLargeError",
    "RateLimitedError",
    "RequestTimeoutError",
    "ResponseParseError",
    "ServerError",
    "UnsupportedUrlError",
    "error_from_status",
]

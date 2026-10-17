"""HTTP 客户端：请求契约、解析、错误归一与重试策略。"""

from __future__ import annotations

import json

import httpx
import pytest

from deepseek_search import errors
from deepseek_search.client import DeepSeekClient

SEARCH_BODY = {
    "results": [
        {
            "title": "DeepSeek 搜索上线",
            "url": "https://example.com/news/1",
            "snippet": "官方发布联网搜索接口。",
            "summary": "接口支持 site: 等语法。",
            "content": "正文内容",
            "site_name": "Example 新闻",
            "date": "2025-09-30T12:00:00Z",
            "rerank_score": 2.3,
            "authority": 4,
        }
    ]
}


def make_client(handler, **overrides):
    """构造注入 MockTransport 的客户端，并记录退避等待。"""

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    kwargs = {
        "transport": httpx.MockTransport(handler),
        "sleep": fake_sleep,
        "log_requests": False,
        "max_retries": 0,
    }
    kwargs.update(overrides)
    client = DeepSeekClient("sk-test-abcd1234", **kwargs)
    return client, waits


async def test_search_posts_expected_payload_and_headers():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=SEARCH_BODY)

    client, _ = make_client(handler)
    try:
        result = await client.search("人工智能", count=3, need_content=True, ori_question="是什么", localization="1.2.3.4")
    finally:
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
    assert captured["auth"] == "Bearer sk-test-abcd1234"
    assert captured["body"] == {
        "query": "人工智能",
        "count": 3,
        "need_content": True,
        "ori_question": "是什么",
        "localization": "1.2.3.4",
    }
    assert result.query == "人工智能"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "DeepSeek 搜索上线"
    assert item.site_name == "Example 新闻"
    assert item.is_high_quality is True
    assert item.is_authoritative is True


async def test_search_omits_optional_fields_and_defaults_count():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    try:
        result = await client.search("query")
    finally:
        await client.aclose()

    assert captured["body"] == {"query": "query", "count": 10, "need_content": False}
    assert result.is_empty is True


async def test_search_truncates_long_query_to_128_chars():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    try:
        await client.search("长" * 200)
    finally:
        await client.aclose()

    assert len(captured["body"]["query"]) == 128


@pytest.mark.parametrize(("count", "expected"), [(999, 50), (-3, 1), (0, 10)])
async def test_search_clamps_count(count, expected):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    try:
        await client.search("q", count=count)
    finally:
        await client.aclose()

    assert captured["body"]["count"] == expected


async def test_search_rejects_blank_query_without_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.InvalidRequestError) as excinfo:
            await client.search("   ")
    finally:
        await client.aclose()

    assert calls == []
    assert "查询为空" in str(excinfo.value)


async def test_fetch_posts_url_and_parses_markdown():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"title": "示例标题", "content": "# 标题\n\n正文"})

    client, _ = make_client(handler)
    try:
        result = await client.fetch("https://example.com/post/1")
    finally:
        await client.aclose()

    assert captured["url"] == "https://api.deepseek.com/v1/tools/fetch"
    assert captured["body"] == {"url": "https://example.com/post/1"}
    assert result.title == "示例标题"
    assert "正文" in result.content
    assert result.url == "https://example.com/post/1"


async def test_fetch_rejects_private_url_without_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"title": "x", "content": "y"})

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.UnsupportedUrlError):
            await client.fetch("http://127.0.0.1/admin")
    finally:
        await client.aclose()

    assert calls == []


@pytest.mark.parametrize(
    ("status", "error_cls"),
    [
        (400, errors.InvalidRequestError),
        (401, errors.AuthenticationError),
        (402, errors.InsufficientBalanceError),
        (403, errors.ContentForbiddenError),
        (404, errors.ContentNotFoundError),
        (408, errors.RequestTimeoutError),
        (413, errors.PayloadTooLargeError),
        (429, errors.RateLimitedError),
        (500, errors.ServerError),
        (502, errors.ServerError),
        (504, errors.ServerError),
    ],
)
async def test_error_status_is_mapped_to_specific_exception(status, error_cls):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "服务端说明", "code": "some_code"}})

    client, _ = make_client(handler)
    try:
        with pytest.raises(error_cls) as excinfo:
            await client.search("q")
    finally:
        await client.aclose()

    assert excinfo.value.status_code == status
    assert excinfo.value.code == "some_code"
    assert "服务端说明" in str(excinfo.value)
    assert excinfo.value.user_message


async def test_retries_server_error_then_succeeds():
    responses = [httpx.Response(500, json={"error": {"message": "boom"}}), httpx.Response(200, json={"results": []})]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls) + 1)
        return responses.pop(0)

    client, waits = make_client(handler, max_retries=1)
    try:
        result = await client.search("q")
    finally:
        await client.aclose()

    assert len(calls) == 2
    assert len(waits) == 1
    assert waits[0] > 0
    assert result.is_empty is True


async def test_retry_budget_exhausted_raises_last_error():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "down"}})

    client, waits = make_client(handler, max_retries=1)
    try:
        with pytest.raises(errors.ServerError):
            await client.search("q")
    finally:
        await client.aclose()

    assert len(calls) == 2
    assert len(waits) == 1


async def test_non_retryable_error_is_not_retried():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client, waits = make_client(handler, max_retries=3)
    try:
        with pytest.raises(errors.AuthenticationError):
            await client.search("q")
    finally:
        await client.aclose()

    assert len(calls) == 1
    assert waits == []


async def test_retry_after_header_is_respected():
    responses = [
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "0.25"}),
        httpx.Response(200, json={"results": []}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client, waits = make_client(handler, max_retries=1)
    try:
        await client.search("q")
    finally:
        await client.aclose()

    assert waits == [pytest.approx(0.25)]


async def test_timeout_is_normalized_to_request_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.RequestTimeoutError):
            await client.search("q")
    finally:
        await client.aclose()


async def test_connection_error_is_normalized_to_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.NetworkError):
            await client.search("q")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["unexpected", "array"]),
    ],
)
async def test_unexpected_response_body_raises_parse_error(response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.ResponseParseError):
            await client.search("q")
    finally:
        await client.aclose()


async def test_fetch_response_without_content_raises_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "", "content": ""})

    client, _ = make_client(handler)
    try:
        with pytest.raises(errors.ResponseParseError):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


def test_client_requires_key_and_valid_base_url():
    with pytest.raises(errors.ConfigurationError):
        DeepSeekClient("")
    with pytest.raises(errors.ConfigurationError):
        DeepSeekClient("sk-x", base_url="ftp://api.deepseek.com")


def test_client_endpoints_and_masked_key():
    client = DeepSeekClient("sk-test-abcd1234", base_url="https://api.deepseek.com/")
    try:
        assert client.search_url == "https://api.deepseek.com/v1/tools/search"
        assert client.fetch_url == "https://api.deepseek.com/v1/tools/fetch"
        assert client.masked_api_key == "sk-***1234"
    finally:
        import asyncio

        asyncio.run(client.aclose())


async def test_aclose_is_idempotent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    await client.aclose()
    await client.aclose()


async def test_connect_timeout_larger_than_total_is_clamped():
    """connect 超时大于总超时不应让 httpx.Timeout 抛 ValueError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    captured: dict = {}
    client = DeepSeekClient(
        "sk-test-abcd1234",
        timeout_seconds=2,
        connect_timeout_seconds=60,
        transport=httpx.MockTransport(handler),
        log_requests=False,
    )
    try:
        result = await client.search("超时钳制")
        assert result.is_empty is True
        assert captured["url"].endswith("/v1/tools/search")
    finally:
        await client.aclose()

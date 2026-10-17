"""URL 安全校验。"""

from __future__ import annotations

import pytest

from deepseek_search.errors import UnsupportedUrlError
from deepseek_search.http import MAX_URL_LENGTH, is_url_like, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/a?b=1#c",
        "http://example.com:8080/path",
        "https://子域名.example.com/中文",
    ],
)
def test_validate_url_accepts_public_http_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "//example.com/a",
        "https://",
        "https://user:pass@example.com/a",
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://router.local/",
        "http://service.internal/",
    ],
)
def test_validate_url_rejects_unsafe_targets(url):
    with pytest.raises(UnsupportedUrlError) as excinfo:
        validate_url(url)
    assert excinfo.value.user_message


def test_validate_url_rejects_too_long_url():
    url = "https://example.com/" + "a" * (MAX_URL_LENGTH + 10)
    with pytest.raises(UnsupportedUrlError):
        validate_url(url)


def test_literal_public_ip_is_allowed():
    assert validate_url("https://8.8.8.8/dns") == "https://8.8.8.8/dns"


def test_is_url_like_recognises_http_prefix():
    assert is_url_like("https://example.com") is True
    assert is_url_like("HTTP://example.com") is True
    assert is_url_like("example.com") is False
    assert is_url_like("") is False

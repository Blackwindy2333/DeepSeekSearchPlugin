"""结果格式化：截断、编号、空结果与错误文案。"""

from __future__ import annotations

from deepseek_search.errors import AuthenticationError, RateLimitedError
from deepseek_search.formatting import (
    format_error,
    format_fetch_result,
    format_search_for_command,
    format_search_result,
    format_status_lines,
    truncate,
)
from deepseek_search.models import FetchResult, SearchResult

from conftest import sample_search_result


def test_truncate_keeps_short_text():
    assert truncate("abc", 10) == "abc"


def test_truncate_appends_suffix_and_respects_limit():
    result = truncate("abcdefgh", 5, suffix="…")
    assert result == "abcd…"
    assert len(result) == 5


def test_truncate_handles_non_positive_and_tiny_limits():
    assert truncate("abcdef", 0) == ""
    assert truncate("abcdef", 1) == "…"


def test_search_result_contains_numbering_links_and_labels():
    text = format_search_result(sample_search_result(count=2))
    assert "找到 2 条" in text
    assert "1. 结果 1" in text
    assert "2. 结果 2" in text
    assert "链接：https://example.com/1" in text
    assert "摘要：" in text
    assert "要点：" in text
    assert "正文节选：" in text
    assert "以上结果由 DeepSeek 搜索 API 提供" in text


def test_search_result_respects_max_results():
    text = format_search_result(sample_search_result(count=3), max_results=1)
    assert "展示前 1 条" in text
    assert "1. 结果 1" in text
    assert "2. 结果 2" not in text


def test_search_result_can_hide_authority_hint_and_tail():
    text = format_search_result(sample_search_result(count=1), show_authority_hint=False, include_tail_hint=False)
    assert "权威度" not in text
    assert "以上结果" not in text


def test_empty_search_result_is_explicit():
    text = format_search_result(SearchResult(query="不存在的词", items=()))
    assert "没有找到" in text
    assert "不存在的词" in text


def test_fetch_result_reports_truncation_and_optional_title():
    result = FetchResult(url="https://example.com/a", title="标题", content="x" * 5000)
    text = format_fetch_result(result, max_content_chars=1000)
    assert "标题：标题" in text
    assert "已截断" in text
    assert "原文 5000 字符" in text

    no_title = format_fetch_result(result, max_content_chars=1000, include_title=False)
    assert "标题：" not in no_title


def test_fetch_result_handles_empty_content():
    text = format_fetch_result(FetchResult(url="https://example.com/a"))
    assert "（未提取到正文）" in text


def test_fetch_result_segmented_view_has_offset_header_and_hint():
    """分段视图：标题展示当前窗口与全文长度，末尾追加 offset 续读提示。"""

    result = FetchResult(
        url="https://example.com/long",
        title="长文",
        content="x" * 150,
        offset=100,
        total_chars=300,
    )
    text = format_fetch_result(result, max_content_chars=150)

    assert "第 101–250 字，共 300 字" in text
    assert "已截断" in text
    assert "继续读取" in text
    assert "offset=250" in text


def test_command_format_is_compact():
    text = format_search_for_command(sample_search_result(count=3), max_results=2)
    assert "搜索结果" in text
    assert "展示 2 条" in text
    assert "https://example.com/1" in text
    assert "https://example.com/3" not in text


def test_empty_command_format():
    assert "没有找到" in format_search_for_command(SearchResult(query="空", items=()))


def test_format_error_uses_user_message_for_known_errors():
    assert format_error(AuthenticationError("细节")) == "DeepSeek API Key 无效或已失效，请检查配置"
    assert format_error(RateLimitedError("细节")) == "搜索请求过于频繁，已被限速，请稍后再试"


def test_format_error_hides_unexpected_details():
    assert format_error(ValueError("secret internal detail")) == "搜索过程中出现未预期的错误，请稍后再试"


def test_status_lines_summarise_counters():
    text = format_status_lines(search_success=3, search_failed=1, fetch_success=2, fetch_failed=0, cache_hit_rate=0.5)
    assert "搜索成功 3 次" in text
    assert "抓取成功 2 次" in text
    assert "50%" in text

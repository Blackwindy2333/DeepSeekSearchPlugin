"""配置模型的默认值、约束与热替换行为。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deepseek_search.config import (
    ApiSection,
    CacheSection,
    DeepSeekSearchConfig,
    LoggingSection,
    PagesSection,
    PluginInfoSection,
    RateLimitSection,
    SearchSection,
)

from conftest import configure


def test_default_values_follow_documented_plan():
    config = DeepSeekSearchConfig()
    assert config.plugin.enabled is True
    assert config.plugin.config_version == "1.0.0"
    assert config.api.base_url == "https://api.deepseek.com"
    assert config.api.timeout_seconds == 20.0
    assert config.api.max_retries == 2
    assert config.search.default_count == 8
    assert config.search.need_content is False
    assert config.fetch.max_content_chars == 4000
    assert config.fetch.include_title is True
    assert config.ratelimit.enabled is True
    assert config.ratelimit.qps == 4.0
    assert config.ratelimit.burst == 2
    assert config.cache.enabled is True
    assert config.cache.search_ttl_seconds == 600
    assert config.cache.fetch_ttl_seconds == 1800
    assert config.cache.max_entries == 256
    assert config.pages.enabled is True
    assert config.pages.ttl_seconds == 86400
    assert config.pages.max_entries == 64
    assert config.pages.max_total_bytes == 8388608
    assert config.logging.level == "INFO"
    assert config.logging.file_enabled is False
    assert config.commands.enabled is True
    assert config.commands.max_results_in_reply == 5


def test_sections_declare_ui_metadata():
    for section in (
        PluginInfoSection,
        ApiSection,
        SearchSection,
        CacheSection,
        PagesSection,
        RateLimitSection,
        LoggingSection,
    ):
        assert getattr(section, "__ui_label__", ""), section.__name__
        assert getattr(section, "__ui_icon__", ""), section.__name__
        assert isinstance(getattr(section, "__ui_order__", None), int), section.__name__


@pytest.mark.parametrize(
    ("section", "payload"),
    [
        (SearchSection, {"default_count": 0}),
        (SearchSection, {"default_count": 51}),
        (ApiSection, {"max_retries": 6}),
        (RateLimitSection, {"qps": 9.0}),
        (CacheSection, {"max_entries": 4}),
        (PagesSection, {"max_entries": 0}),
        (PagesSection, {"max_total_bytes": 1024}),
        (PagesSection, {"ttl_seconds": 10}),
        (LoggingSection, {"level": "TRACE"}),
    ],
)
def test_sections_reject_out_of_range_values(section, payload):
    with pytest.raises(ValidationError):
        section(**payload)


def test_pages_section_accepts_boundary_values():
    section = PagesSection(max_entries=1, max_total_bytes=1048576, ttl_seconds=60)
    assert section.max_entries == 1
    assert section.max_total_bytes == 1048576
    assert section.ttl_seconds == 60

    upper = PagesSection(max_entries=2000, max_total_bytes=268435456, ttl_seconds=604800)
    assert upper.max_entries == 2000
    assert upper.max_total_bytes == 268435456
    assert upper.ttl_seconds == 604800


def test_section_assignment_validates():
    section = SearchSection()
    with pytest.raises(ValidationError):
        section.default_count = 0


def test_config_ignores_unknown_fields_for_forward_compatibility():
    config = SearchSection(default_count=5, future_field="ignored")
    assert config.default_count == 5


def test_plugin_config_roundtrip_through_sdk(plugin_module):
    instance = plugin_module.create_plugin()
    configure(
        instance,
        api={"api_key": "sk-demo-1234"},
        search={"default_count": 3, "need_content": True},
        commands={"enabled": False},
    )
    assert instance.config.api.api_key == "sk-demo-1234"
    assert instance.config.search.default_count == 3
    assert instance.config.search.need_content is True
    assert instance.config.commands.enabled is False

    data = instance.get_plugin_config_data()
    assert data["plugin"]["config_version"] == "1.0.0"
    assert data["search"]["default_count"] == 3
    assert data["api"]["api_key"] == "sk-demo-1234"

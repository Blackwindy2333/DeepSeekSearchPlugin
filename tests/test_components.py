"""组件层：@Tool / @Command / @API / @HomeCard 的实际返回值契约。

这些用例直接调用组件方法，不经过 Host，因此断言的是「交给 Host 的字典/三元组」
结构——Host 会把 ``content`` 喂给 LLM，把命令三元组转成聊天回复。
"""

from __future__ import annotations

import pytest

from conftest import configure


async def test_search_tool_returns_success_payload(plugin_with_fake_client):
    plugin, fake = plugin_with_fake_client

    result = await plugin.search_web(query="人工智能", count=2)

    assert result["success"] is True
    assert "找到" in result["content"]
    assert result["result_count"] == 2
    assert result["cached"] is False
    assert fake.search_calls[0]["query"] == "人工智能"
    assert fake.search_calls[0]["count"] == 2


async def test_search_tool_coerces_llm_typed_arguments(plugin_with_fake_client):
    plugin, fake = plugin_with_fake_client

    result = await plugin.search_web(query="  人工智能  ", count="3", need_content="false", ori_question="原问题")

    assert result["success"] is True
    assert fake.search_calls[0]["count"] == 3
    assert fake.search_calls[0]["need_content"] is False
    assert fake.search_calls[0]["ori_question"] == "原问题"


async def test_search_tool_reports_configuration_error(plugin_module, fake_ctx):
    instance = plugin_module.create_plugin()
    configure(instance, ratelimit={"enabled": False})
    instance._set_context(fake_ctx("plugin.test.no-key"))
    await instance.on_load()
    try:
        result = await instance.search_web(query="没有 Key")
    finally:
        await instance.on_unload()

    assert result["success"] is False
    assert "尚未配置 DeepSeek API Key" in result["content"]


async def test_fetch_tool_returns_content(plugin_with_fake_client):
    plugin, fake = plugin_with_fake_client

    result = await plugin.fetch_webpage(url="https://example.com/1")

    assert result["success"] is True
    assert "标题：示例页面" in result["content"]
    assert result["url"] == "https://example.com/1"
    assert fake.fetch_calls == ["https://example.com/1"]


async def test_search_command_returns_triple(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client

    success, response, weight = await plugin.cmd_search(matched_groups={"query": "人工智能"})

    assert success is True
    assert "搜索结果" in response
    assert "https://example.com/1" in response
    assert weight == 0


async def test_search_command_without_query_returns_usage(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client

    success, response, _ = await plugin.cmd_search(matched_groups={})

    assert success is False
    assert "/search" in response


async def test_fetch_command_returns_triple(plugin_with_fake_client):
    plugin, fake = plugin_with_fake_client

    success, response, weight = await plugin.cmd_fetch(matched_groups={"url": "https://example.com/1"})

    assert success is True
    assert "示例页面" in response
    assert weight == 0
    assert fake.fetch_calls == ["https://example.com/1"]


async def test_fetch_command_without_url_returns_usage(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client

    success, response, _ = await plugin.cmd_fetch(matched_groups={})

    assert success is False
    assert "/fetch" in response


async def test_commands_respect_enabled_switch(plugin_with_fake_client):
    plugin, fake = plugin_with_fake_client
    plugin.config.commands.enabled = False

    assert (await plugin.cmd_search(matched_groups={"query": "x"}))[0] is False
    assert (await plugin.cmd_fetch(matched_groups={"url": "https://example.com/1"}))[0] is False
    assert fake.search_calls == []
    assert fake.fetch_calls == []


async def test_api_search_returns_structured_results(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client

    payload = await plugin.api_search(query="插件间调用", count=1)

    assert payload["success"] is True
    assert payload["results"][0]["title"] == "结果 1"
    assert payload["results"][0]["url"] == "https://example.com/1"


async def test_api_get_stats_reports_ready_state(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client
    await plugin.search_web(query="统计")

    payload = await plugin.api_get_stats()

    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["metrics"]["search"]["success"] == 1


async def test_api_get_stats_before_load_is_safe(plugin_module):
    instance = plugin_module.create_plugin()

    payload = await instance.api_get_stats()

    assert payload["ok"] is False
    assert "尚未完成加载" in payload["error"]


async def test_home_card_handler_is_a_placeholder(plugin_with_fake_client):
    plugin, _ = plugin_with_fake_client
    assert await plugin.home_status() == {"ok": True}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("7", 7), (7, 7), (True, None), ("abc", None)],
)
def test_coerce_int(plugin_module, raw, expected):
    assert plugin_module._coerce_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("true", True), ("False", False), ("1", True), ("off", False), (1, True), ("maybe", None)],
)
def test_coerce_bool(plugin_module, raw, expected):
    assert plugin_module._coerce_bool(raw) == expected


async def test_fetch_command_caps_reply_length(plugin_with_fake_client):
    """聊天命令回复的正文有长度上限（工具调用不受此限），避免刷屏。"""

    from deepseek_search.models import FetchResult

    plugin, fake = plugin_with_fake_client
    fake.fetch_result = FetchResult(url="https://example.com/long", title="长页", content="正" * 6000)

    success, response, _ = await plugin.cmd_fetch(matched_groups={"url": "https://example.com/long"})

    assert success is True
    assert "已截断" in response
    assert len(response) < 3000

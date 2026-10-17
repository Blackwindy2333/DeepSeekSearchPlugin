"""生命周期：加载、卸载、配置热更新与环境变量覆盖。"""

from __future__ import annotations

import logging

from deepseek_search.settings import Settings

from conftest import configure


async def test_on_load_builds_service(loaded_plugin):
    assert loaded_plugin._service is not None
    snapshot = loaded_plugin._service.snapshot()
    assert snapshot["ready"] is True
    assert snapshot["api"]["configured"] is True


async def test_on_load_warns_when_api_key_missing(plugin_module, fake_ctx, caplog):
    instance = plugin_module.create_plugin()
    configure(instance, logging={"file_enabled": False})
    instance._set_context(fake_ctx("plugin.test.lifecycle.warn"))

    with caplog.at_level(logging.INFO, logger="plugin.test.lifecycle.warn"):
        await instance.on_load()
    try:
        assert "尚未配置 DeepSeek API Key" in caplog.text
    finally:
        await instance.on_unload()


async def test_on_unload_releases_service_and_is_idempotent(plugin_module, fake_ctx):
    instance = plugin_module.create_plugin()
    configure(instance, logging={"file_enabled": False})
    instance._set_context(fake_ctx("plugin.test.lifecycle.unload"))
    await instance.on_load()

    await instance.on_unload()
    assert instance._service is None

    await instance.on_unload()
    assert instance._service is None


async def test_config_update_self_refreshes_service_in_place(plugin_module, fake_ctx):
    instance = plugin_module.create_plugin()
    configure(
        instance,
        api={"api_key": "sk-test-1234"},
        ratelimit={"enabled": False},
        logging={"file_enabled": False},
    )
    instance._set_context(fake_ctx("plugin.test.lifecycle.reload"))
    await instance.on_load()
    try:
        before = instance._service
        await instance.on_config_update("self", {}, "1.0.1")
        assert instance._service is before

        await instance.on_config_update("bot", {}, "1.0.1")
        assert instance._service is before
    finally:
        await instance.on_unload()


async def test_config_update_creates_service_when_missing(plugin_module, fake_ctx):
    instance = plugin_module.create_plugin()
    configure(instance, logging={"file_enabled": False})
    instance._set_context(fake_ctx("plugin.test.lifecycle.late"))

    await instance.on_config_update("self", {}, "1.0.0")

    assert instance._service is not None
    await instance.on_unload()


async def test_hot_update_can_enable_and_detach_file_logging(plugin_module, fake_ctx, tmp_path):
    instance = plugin_module.create_plugin()
    configure(
        instance,
        api={"api_key": "sk-test-1234"},
        ratelimit={"enabled": False},
        logging={"file_enabled": False},
    )
    instance._set_context(fake_ctx("plugin.test.lifecycle.filelog"))
    await instance.on_load()
    try:
        instance.config.logging.file_enabled = True
        await instance.on_config_update("self", {}, "1.0.2")
        log_file = tmp_path / "data" / "logs" / "dsp.log"
        assert log_file.exists()
        assert instance._log_file == log_file

        instance.config.logging.file_enabled = False
        await instance.on_config_update("self", {}, "1.0.3")
        assert instance._log_file is None
    finally:
        await instance.on_unload()


async def test_env_overrides_win_over_config(plugin_module, fake_ctx, monkeypatch):
    monkeypatch.setattr(
        plugin_module,
        "env_settings",
        Settings(api_key="sk-env-abcd", log_level="DEBUG", log_file_enabled=False),
    )
    instance = plugin_module.create_plugin()
    configure(instance, api={"api_key": "sk-config-9999"}, ratelimit={"enabled": False})
    instance._set_context(fake_ctx("plugin.test.lifecycle.env"))
    await instance.on_load()
    try:
        assert instance._service.ready is True
        assert instance._service.snapshot()["api"]["source"] == "env"
        assert instance.ctx.logger.level == logging.DEBUG
    finally:
        await instance.on_unload()


async def test_plugin_config_is_typed_and_has_defaults(loaded_plugin):
    config = loaded_plugin.config
    assert config.plugin.enabled is True
    assert config.search.default_count == 8
    assert config.fetch.max_content_chars == 4000
    assert config.api.base_url == "https://api.deepseek.com"

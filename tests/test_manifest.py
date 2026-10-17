"""Manifest、目录结构与组件注册契约。"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from maibot_sdk import MaiBotPlugin

from deepseek_search.config import DeepSeekSearchConfig

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "_manifest.json"


def load_manifest() -> dict:
    """读取并解析仓库根目录的 manifest。"""

    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_is_valid_json_with_required_identity():
    manifest = load_manifest()
    assert manifest["manifest_version"] == 2
    assert re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", manifest["id"]), manifest["id"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), manifest["version"]
    assert manifest["name"]
    assert manifest["description"]
    assert manifest["license"] == "GPL-3.0"


def test_manifest_author_and_repository_are_expected():
    manifest = load_manifest()
    author = manifest["author"]
    assert author["name"] == "BlackBee114514"
    assert author["url"] == "https://space.bilibili.com/1041748349"
    assert manifest["urls"]["repository"] == "https://github.com/Blackwindy2333/DeepSeekSearchPlugin"


def test_manifest_declares_no_capabilities_but_httpx_dependency():
    manifest = load_manifest()
    assert manifest["capabilities"] == []
    dependencies = manifest["dependencies"]
    assert any(item["type"] == "python_package" and item["name"] == "httpx" for item in dependencies)
    assert all(item["version_spec"] for item in dependencies)
    assert manifest["plugin_type"] == "integration"
    assert manifest["i18n"]["default_locale"] == "zh-CN"


def test_manifest_compatibility_ranges_cover_current_host():
    manifest = load_manifest()
    host = manifest["host_application"]
    sdk = manifest["sdk"]
    assert host["min_version"] == "1.3.0"
    assert host["max_version"] == "999.999.999"
    assert sdk["min_version"] == "2.14.0"
    assert sdk["max_version"] == "999.999.999"


def test_plugin_directory_has_required_files():
    for name in ("_manifest.json", "plugin.py", "LICENSE", ".gitignore"):
        assert (ROOT / name).is_file(), f"缺少 {name}"
    for name in ("README.md", "requirements-dev.txt"):
        assert (ROOT / name).is_file(), f"缺少 {name}"


def test_create_plugin_returns_sdk_plugin_with_config_model(plugin_module):
    instance = plugin_module.create_plugin()
    assert isinstance(instance, MaiBotPlugin)
    assert instance.config_model is DeepSeekSearchConfig
    assert Path(plugin_module.__file__).resolve().parent == ROOT
    assert str(ROOT) in sys.path


def test_plugin_registers_expected_components(plugin_module):
    instance = plugin_module.create_plugin()
    components = instance.get_components()
    counts = Counter(item["type"] for item in components)
    assert counts == {"API": 2, "COMMAND": 2, "HOME_CARD": 1, "TOOL": 2}

    tools = {item["name"]: item["metadata"] for item in components if item["type"] == "TOOL"}
    assert set(tools) == {"search_web", "fetch_webpage"}
    assert tools["search_web"]["handler_name"] == "search_web"

    commands = {item["name"]: item["metadata"] for item in components if item["type"] == "COMMAND"}
    assert set(commands) == {"search", "fetch"}
    assert all(meta["command_pattern"].startswith("^") for meta in commands.values())

    apis = {item["name"]: item["metadata"] for item in components if item["type"] == "API"}
    assert set(apis) == {"search", "get_stats"}
    assert all(meta["public"] is True for meta in apis.values())


def test_tool_parameters_are_documented_for_the_model(plugin_module):
    instance = plugin_module.create_plugin()
    tools = {item["name"]: item["metadata"] for item in instance.get_components() if item["type"] == "TOOL"}

    search_params = {param["name"]: param for param in tools["search_web"]["parameters"]}
    assert search_params["query"]["required"] is True
    assert search_params["query"]["param_type"] == "string"
    assert search_params["count"]["param_type"] == "integer"
    assert search_params["count"]["required"] is False
    assert search_params["need_content"]["param_type"] == "boolean"

    fetch_params = {param["name"]: param for param in tools["fetch_webpage"]["parameters"]}
    assert fetch_params["url"]["required"] is True
    assert fetch_params["max_chars"]["param_type"] == "integer"


def test_command_patterns_match_documented_usage(plugin_module):
    instance = plugin_module.create_plugin()
    commands = {item["name"]: item["metadata"] for item in instance.get_components() if item["type"] == "COMMAND"}

    search_pattern = re.compile(commands["search"]["command_pattern"])
    assert search_pattern.match("/search 人工智能")
    assert search_pattern.match("/搜索 人工智能")
    assert not search_pattern.match("请帮我搜索")

    fetch_pattern = re.compile(commands["fetch"]["command_pattern"])
    assert fetch_pattern.match("/fetch https://example.com/a")
    assert fetch_pattern.match("/抓取 https://example.com/a")
    # 只输入命令时交给处理器回用法提示，因此是匹配而不是不匹配
    assert fetch_pattern.match("/fetch")
    assert not fetch_pattern.match("/fetchx https://example.com/a")


def test_home_card_links_to_plugin_config_page(plugin_module):
    instance = plugin_module.create_plugin()
    card = next(item for item in instance.get_components() if item["type"] == "HOME_CARD")
    assert card["name"] == "status"
    assert card["metadata"]["link_url"].startswith("/plugin-config?plugin=")
    assert card["metadata"]["icon"] == "search"

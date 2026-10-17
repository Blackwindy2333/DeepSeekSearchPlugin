"""pytest 公共夹具与测试替身。

测试原则：

- 不访问真实 DeepSeek API：网络层用 ``httpx.MockTransport``，业务层用 ``FakeClient``；
- 不写真实 MaiBot 数据目录：``ctx.paths.data_dir`` 指向 ``tmp_path``；
- 每个用例自己造配置，避免相互污染。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_search.config import DeepSeekSearchConfig  # noqa: E402
from deepseek_search.models import FetchResult, SearchItem, SearchResult  # noqa: E402


def make_config(**sections: dict[str, Any]) -> DeepSeekSearchConfig:
    """构造默认配置，并按 ``节名=字段字典`` 覆盖字段。

    Args:
        **sections: 例如 ``ratelimit={"enabled": False}``。

    Returns:
        DeepSeekSearchConfig: 覆盖后的配置实例。
    """

    config = DeepSeekSearchConfig()
    for section_name, values in sections.items():
        section = getattr(config, section_name)
        for field_name, value in values.items():
            setattr(section, field_name, value)
    return config


def configure(instance, **sections: dict[str, Any]) -> None:
    """用 SDK 默认配置 + 覆盖项配置插件实例。

    Host 侧传入的配置来自 ``config.toml``，一定包含 ``[plugin].config_version``；
    测试里直接拼一个「只有变更项」的字典会触发 SDK 的版本校验，因此统一走这个 helper。

    Args:
        instance: 插件实例。
        **sections: 例如 ``api={"api_key": "sk-test"}``。
    """

    data = instance.build_default_config()
    for section_name, values in sections.items():
        section = data.get(section_name)
        if not isinstance(section, dict):
            section = {}
            data[section_name] = section
        section.update(values)
    instance.set_plugin_config(data)


def sample_search_result(query: str = "DeepSeek 搜索", count: int = 2) -> SearchResult:
    """构造一份可断言的结果集。"""

    items = [
        SearchItem(
            title=f"结果 {index}",
            url=f"https://example.com/{index}",
            snippet="摘要" * 10,
            summary="要点" * 20,
            content="正文" * 30,
            site_name="Example",
            date="2025-01-01T00:00:00Z",
            rerank_score=2.0,
            authority=4,
        )
        for index in range(1, count + 1)
    ]
    return SearchResult(query=query, items=tuple(items), requested_count=count)


class FakeClient:
    """``DeepSeekClient`` 的测试替身。"""

    def __init__(
        self,
        *,
        search_result: SearchResult | None = None,
        fetch_result: FetchResult | None = None,
        search_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
    ) -> None:
        self.search_result = search_result if search_result is not None else sample_search_result()
        self.fetch_result = fetch_result or FetchResult(
            url="https://example.com/1",
            title="示例页面",
            content="正文" * 100,
        )
        self.search_error = search_error
        self.fetch_error = fetch_error
        self.search_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[str] = []
        self.closed = False
        self.base_url = "https://api.deepseek.com"
        self.masked_api_key = "sk-***1234"

    async def search(
        self,
        query: str,
        *,
        count: int = 10,
        need_content: bool = False,
        ori_question: str | None = None,
        localization: str = "",
    ) -> SearchResult:
        """记录调用并返回预置结果或抛出预置异常。"""

        self.search_calls.append(
            {
                "query": query,
                "count": count,
                "need_content": need_content,
                "ori_question": ori_question,
                "localization": localization,
            }
        )
        if self.search_error is not None:
            raise self.search_error
        return self.search_result

    async def fetch(self, url: str) -> FetchResult:
        """记录调用并返回预置结果或抛出预置异常。"""

        self.fetch_calls.append(url)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_result

    async def aclose(self) -> None:
        """记录关闭。"""

        self.closed = True


@pytest.fixture()
def fake_ctx(tmp_path):
    """返回一个构造最小 ``ctx`` 的工厂（logger + paths）。"""

    def _create(name: str = "plugin.test.deepseek-search"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        return SimpleNamespace(
            logger=logger,
            paths=SimpleNamespace(data_dir=tmp_path / "data", runtime_dir=tmp_path / "temp"),
        )

    return _create


@pytest.fixture()
def plugin_module():
    """导入插件入口模块。"""

    import plugin

    return plugin


@pytest.fixture()
async def loaded_plugin(plugin_module, fake_ctx):
    """已完成 ``on_load`` 的插件实例（配置了假 Key、关闭限速）。"""

    instance = plugin_module.create_plugin()
    configure(
        instance,
        api={"api_key": "sk-test-1234"},
        ratelimit={"enabled": False},
        logging={"file_enabled": False},
    )
    instance._set_context(fake_ctx())
    await instance.on_load()
    try:
        yield instance
    finally:
        await instance.on_unload()


@pytest.fixture()
async def plugin_with_fake_client(loaded_plugin):
    """把已加载插件的 HTTP 客户端替换成 ``FakeClient``。"""

    fake = FakeClient()
    service = loaded_plugin._service
    service._client_factory = lambda effective, logger: fake
    service._client = None
    service._client_signature = ()
    return loaded_plugin, fake

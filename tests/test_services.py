"""业务编排层：缓存、失败归一、热重载与状态快照。"""

from __future__ import annotations

from deepseek_search.errors import AuthenticationError, UnsupportedUrlError
from deepseek_search.services import DeepSeekSearchService
from deepseek_search.settings import Settings

from conftest import FakeClient, make_config, sample_search_result


def make_service(config=None, fake=None, env=None):
    """构造注入了假客户端的 service。"""

    config = config or make_config(ratelimit={"enabled": False})
    fake = fake or FakeClient()
    factory_calls: list[dict] = []

    def factory(effective, logger):
        factory_calls.append(effective)
        return fake

    service = DeepSeekSearchService(config, env=env or Settings(), client_factory=factory)
    return service, fake, factory_calls


async def test_search_success_returns_llm_readable_content():
    service, fake, _ = make_service()
    outcome = await service.search("人工智能", count=2)

    assert outcome.success is True
    assert outcome.cached is False
    assert "找到" in outcome.content
    assert "https://example.com/1" in outcome.content
    assert fake.search_calls[0]["query"] == "人工智能"
    assert fake.search_calls[0]["count"] == 2
    assert outcome.to_dict()["result_count"] == 2

    metrics = service.snapshot()["metrics"]
    assert metrics["search"]["success"] == 1
    assert metrics["search"]["requests"] == 1


async def test_identical_search_is_served_from_cache():
    service, fake, _ = make_service()
    first = await service.search("缓存测试")
    second = await service.search("缓存测试")

    assert first.cached is False
    assert second.cached is True
    assert len(fake.search_calls) == 1
    assert second.content == first.content
    assert service.snapshot()["metrics"]["cache"]["hits"] == 1


async def test_different_parameters_bypass_cache():
    service, fake, _ = make_service()
    await service.search("参数测试", count=1)
    await service.search("参数测试", count=2)
    assert len(fake.search_calls) == 2


async def test_cache_disabled_always_calls_api():
    config = make_config(ratelimit={"enabled": False}, cache={"enabled": False})
    service, fake, _ = make_service(config=config)
    await service.search("无缓存")
    await service.search("无缓存")
    assert len(fake.search_calls) == 2


async def test_search_failure_is_returned_as_readable_message():
    fake = FakeClient(search_error=AuthenticationError("bad key", status_code=401))
    service, _, _ = make_service(fake=fake)
    outcome = await service.search("失败测试")

    assert outcome.success is False
    assert outcome.error == "DeepSeek API Key 无效或已失效，请检查配置"
    assert "搜索失败" in outcome.content
    metrics = service.snapshot()["metrics"]
    assert metrics["search"]["failed"] == 1
    assert metrics["last_error"]


async def test_unexpected_exception_is_contained():
    fake = FakeClient(search_error=RuntimeError("boom"))
    service, _, _ = make_service(fake=fake)
    outcome = await service.search("异常测试")

    assert outcome.success is False
    assert "搜索过程中出现未预期的错误" in outcome.content


async def test_missing_api_key_is_reported_without_network():
    config = make_config(ratelimit={"enabled": False})
    service = DeepSeekSearchService(config, env=Settings())
    outcome = await service.search("没有 Key")

    assert outcome.success is False
    assert "尚未配置 DeepSeek API Key" in outcome.content
    assert service.ready is False


async def test_env_api_key_overrides_config():
    service = DeepSeekSearchService(
        make_config(ratelimit={"enabled": False}),
        env=Settings(api_key="sk-env-abcd"),
        client_factory=lambda effective, logger: FakeClient(),
    )
    assert service.ready is True
    snapshot = service.snapshot()
    assert snapshot["api"]["source"] == "env"


async def test_fetch_truncates_result_and_caches():
    long_content = "正文" * 3000
    fake = FakeClient(fetch_result=None)
    fake.fetch_result = type("_F", (), {})()  # 占位，下面直接构造 dataclass
    from deepseek_search.models import FetchResult

    fake.fetch_result = FetchResult(url="https://example.com/a", title="长文", content=long_content)
    service, _, _ = make_service(fake=fake)

    outcome = await service.fetch("https://example.com/a", max_chars=500)
    assert outcome.success is True
    assert outcome.result is not None
    assert outcome.result.truncated is True
    assert "已截断" in outcome.content
    assert fake.fetch_calls == ["https://example.com/a"]

    cached = await service.fetch("https://example.com/a", max_chars=500)
    assert cached.cached is True
    assert len(fake.fetch_calls) == 1


async def test_fetch_failure_is_readable():
    fake = FakeClient(fetch_error=UnsupportedUrlError("bad"))
    service, _, _ = make_service(fake=fake)
    outcome = await service.fetch("not-a-url")
    assert outcome.success is False
    assert "抓取失败" in outcome.content
    assert outcome.error


async def test_apply_config_resizes_cache_and_reports_changes():
    config = make_config(ratelimit={"enabled": False}, cache={"max_entries": 256})
    service, _, _ = make_service(config=config)

    new_config = make_config(ratelimit={"enabled": False}, cache={"max_entries": 32})
    changes = await service.apply_config(new_config)

    assert "cache" in changes
    assert service.snapshot()["cache"]["max_entries"] == 32


async def test_apply_config_rebuilds_client_when_api_settings_change():
    config = make_config(ratelimit={"enabled": False})
    service, fake, factory_calls = make_service(config=config)
    await service.search("重建前")
    assert len(factory_calls) == 1

    new_config = make_config(ratelimit={"enabled": False}, api={"base_url": "https://api.example.com"})
    changes = await service.apply_config(new_config)
    assert changes["client"] == "rebuilt"
    assert fake.closed is True

    await service.search("重建后")
    assert len(factory_calls) == 2


async def test_snapshot_exposes_all_sections():
    service, _, _ = make_service()
    snapshot = service.snapshot()
    assert set(snapshot) == {"ready", "api", "metrics", "cache", "ratelimit", "config"}
    assert snapshot["api"]["base_url"] == "https://api.deepseek.com"
    assert snapshot["config"]["search_default_count"] == 8
    assert snapshot["ratelimit"] is None  # 该配置关闭了限速


async def test_aclose_releases_client():
    service, fake, _ = make_service()
    await service.search("关闭测试")
    await service.aclose()
    assert fake.closed is True


async def test_rate_limit_toggle_rebuilds_client_with_new_bucket():
    """限速开关热切换后，客户端必须绑定新桶（否则旧桶限速继续生效）。"""

    config = make_config(ratelimit={"enabled": False})
    service, _, factory_calls = make_service(config=config)
    await service.search("限速关闭")
    assert len(factory_calls) == 1
    assert service.snapshot()["ratelimit"] is None

    new_config = make_config(ratelimit={"enabled": True, "qps": 2.0, "burst": 1})
    changes = await service.apply_config(new_config)

    assert changes["client"] == "rebuilt"
    assert service.snapshot()["ratelimit"]["qps"] == 2.0
    await service.search("限速打开")
    assert len(factory_calls) == 2


async def test_fetch_cache_hit_recomputes_truncation_for_new_limit():
    """缓存正文按本次 limit 重新判定 truncated，而不是沿用首次请求的标记。"""

    from deepseek_search.models import FetchResult

    long_content = "正" * 6000
    fake = FakeClient(
        fetch_result=FetchResult(url="https://example.com/long", title="长文", content=long_content),
    )
    service, _, _ = make_service(fake=fake)

    first = await service.fetch("https://example.com/long", max_chars=6000)
    assert first.result is not None
    assert first.result.truncated is False

    cached = await service.fetch("https://example.com/long", max_chars=500)
    assert cached.cached is True
    assert cached.result is not None
    assert cached.result.truncated is True
    assert "已截断" in cached.content
    assert len(fake.fetch_calls) == 1


async def test_closed_service_rejects_new_requests():
    """aclose() 之后不允许悄悄创建新客户端复活服务。"""

    service, fake, _ = make_service()
    await service.search("关闭前")
    await service.aclose()

    outcome = await service.search("关闭后")
    assert outcome.success is False
    assert outcome.error
    assert fake.closed is True

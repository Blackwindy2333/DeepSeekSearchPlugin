"""插件配置模型。

约定（与 MaiBot 插件配置规范一致）：

1. ``config_model`` 是 ``MaiBotPlugin`` 上的类属性，Host 用它生成 WebUI 配置表单与默认 ``config.toml``；
2. 每个字段都必须有默认值，否则 Runner 无法生成默认配置；
3. 保留 ``[plugin]`` 分组，其中必须含 ``enabled`` 与 ``config_version``；
4. 只放**配置**，不放运行期状态（热重载时会整体替换）。
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from maibot_sdk.config import PluginConfigBase

#: 配置节通用设置：允许赋值校验、忽略未知字段，避免降级安装时因旧配置报错
_SECTION_CONFIG = ConfigDict(validate_assignment=True, extra="ignore")


class PluginInfoSection(BaseModel):
    """插件自身开关与配置版本。"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    model_config = _SECTION_CONFIG

    enabled: bool = Field(default=True, description="是否启用本插件")
    config_version: str = Field(default="1.0.0", description="配置版本，供 Host 做迁移判断，请勿手改")


class ApiSection(BaseModel):
    """DeepSeek API 接入参数。"""

    __ui_label__: ClassVar[str] = "API 接入"
    __ui_icon__: ClassVar[str] = "key"
    __ui_order__: ClassVar[int] = 10

    model_config = _SECTION_CONFIG

    api_key: str = Field(
        default="",
        description="DeepSeek API Key（sk- 开头），仅保存在 config.toml，请勿提交到仓库",
        json_schema_extra={"placeholder": "sk-...", "secret": True},
    )
    base_url: str = Field(
        default="https://api.deepseek.com",
        description="API 基础地址，一般无需修改",
        json_schema_extra={"placeholder": "https://api.deepseek.com"},
    )
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0, description="单次请求总超时（秒）")
    connect_timeout_seconds: float = Field(default=5.0, ge=1.0, le=60.0, description="建立连接超时（秒）")
    max_retries: int = Field(default=2, ge=0, le=5, description="可重试错误（408/429/5xx/网络异常）的最大重试次数")
    retry_backoff_seconds: float = Field(default=1.5, ge=0.1, le=30.0, description="首次重试等待（秒），之后指数退避")
    retry_max_backoff_seconds: float = Field(default=8.0, ge=0.5, le=120.0, description="单次重试等待上限（秒）")
    proxy: str = Field(
        default="",
        description="HTTP 代理，如 http://127.0.0.1:7890；留空表示直连",
        json_schema_extra={"placeholder": "http://127.0.0.1:7890"},
    )


class SearchSection(BaseModel):
    """联网搜索（Search）默认参数。"""

    __ui_label__: ClassVar[str] = "搜索"
    __ui_icon__: ClassVar[str] = "search"
    __ui_order__: ClassVar[int] = 20

    model_config = _SECTION_CONFIG

    default_count: int = Field(default=8, ge=1, le=50, description="默认返回结果条数（1–50）")
    need_content: bool = Field(default=False, description="默认是否附带网页正文（会显著增大响应体）")
    localization: str = Field(default="", description="本地化倾向（IP 字符串），留空表示不指定")
    max_ori_question_chars: int = Field(default=128, ge=0, le=128, description="原始问题长度上限（超过自动截断）")
    snippet_max_chars: int = Field(default=200, ge=50, le=2000, description="给模型看的摘要片段截断长度")
    summary_max_chars: int = Field(default=500, ge=100, le=2000, description="结构化摘要截断长度")
    content_max_chars: int = Field(default=1200, ge=100, le=8000, description="单条结果正文截断长度")
    max_results_in_text: int = Field(default=8, ge=1, le=20, description="格式化文本中最多展示的结果条数")
    show_authority_hint: bool = Field(default=True, description="是否在结果中标注站点权威度提示")


class FetchSection(BaseModel):
    """网页抓取（Fetch）默认参数。"""

    __ui_label__: ClassVar[str] = "网页抓取"
    __ui_icon__: ClassVar[str] = "file-text"
    __ui_order__: ClassVar[int] = 30

    model_config = _SECTION_CONFIG

    max_content_chars: int = Field(default=4000, ge=200, le=30000, description="返回正文的最大字符数，超出截断")
    include_title: bool = Field(default=True, description="返回内容中是否包含网页标题")


class RateLimitSection(BaseModel):
    """本地限速（令牌桶）。"""

    __ui_label__: ClassVar[str] = "限速"
    __ui_icon__: ClassVar[str] = "gauge"
    __ui_order__: ClassVar[int] = 40

    model_config = _SECTION_CONFIG

    enabled: bool = Field(default=True, description="是否启用本地限速")
    qps: float = Field(default=4.0, ge=0.1, le=5.0, description="每秒允许的请求数（官方上限 5 QPS，留出余量）")
    burst: int = Field(default=2, ge=1, le=5, description="允许的瞬时突发请求数")


class CacheSection(BaseModel):
    """结果缓存（TTL + LRU）。"""

    __ui_label__: ClassVar[str] = "缓存"
    __ui_icon__: ClassVar[str] = "database"
    __ui_order__: ClassVar[int] = 50

    model_config = _SECTION_CONFIG

    enabled: bool = Field(default=True, description="是否启用结果缓存")
    search_ttl_seconds: int = Field(default=600, ge=0, le=86400, description="搜索缓存有效期（秒）")
    fetch_ttl_seconds: int = Field(default=1800, ge=0, le=86400, description="抓取缓存有效期（秒）")
    max_entries: int = Field(default=256, ge=16, le=4096, description="缓存条数上限（超出淘汰最久未使用的）")


class PagesSection(BaseModel):
    """网页正文磁盘缓存（支撑长网页分段续读）。"""

    __ui_label__: ClassVar[str] = "页面磁盘缓存"
    __ui_icon__: ClassVar[str] = "hard-drive"
    __ui_order__: ClassVar[int] = 55

    model_config = _SECTION_CONFIG

    enabled: bool = Field(default=True, description="是否把抓取到的完整正文缓存到 data 目录，供分段续读复用")
    ttl_seconds: int = Field(default=86400, ge=60, le=604800, description="磁盘缓存有效期（秒），过期自动清理")
    max_entries: int = Field(default=64, ge=1, le=2000, description="磁盘缓存最多保存的页面数")
    max_total_bytes: int = Field(default=8388608, ge=1048576, le=268435456, description="磁盘缓存总大小上限（字节，默认 8 MB）")


class LoggingSection(BaseModel):
    """日志策略。"""

    __ui_label__: ClassVar[str] = "日志"
    __ui_icon__: ClassVar[str] = "scroll-text"
    __ui_order__: ClassVar[int] = 60

    model_config = _SECTION_CONFIG

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", description="日志级别")
    log_requests: bool = Field(default=True, description="是否记录每次请求的关键字段")
    log_response_preview_chars: int = Field(default=200, ge=0, le=2000, description="响应预览长度，0 表示不记录预览")
    file_enabled: bool = Field(default=False, description="是否额外写入插件 data 目录下的日志文件")
    file_max_bytes: int = Field(default=2097152, ge=65536, le=104857600, description="单个日志文件大小上限（字节）")
    file_backup_count: int = Field(default=3, ge=0, le=20, description="日志滚动保留的文件个数")


class CommandsSection(BaseModel):
    """聊天命令行为。"""

    __ui_label__: ClassVar[str] = "命令"
    __ui_icon__: ClassVar[str] = "terminal"
    __ui_order__: ClassVar[int] = 70

    model_config = _SECTION_CONFIG

    enabled: bool = Field(default=True, description="是否启用 /search 与 /fetch 命令")
    max_results_in_reply: int = Field(default=5, ge=1, le=20, description="命令回复中最多展示的结果条数")


class DeepSeekSearchConfig(PluginConfigBase):
    """插件配置根模型：每个字段对应 ``config.toml`` 的一个 ``[section]``。"""

    __ui_label__: ClassVar[str] = "DeepSeek 联网搜索与网页抓取"
    __ui_icon__: ClassVar[str] = "globe"
    __ui_order__: ClassVar[int] = 0

    plugin: PluginInfoSection = Field(default_factory=PluginInfoSection, description="插件开关")
    api: ApiSection = Field(default_factory=ApiSection, description="DeepSeek API 接入")
    search: SearchSection = Field(default_factory=SearchSection, description="搜索默认参数")
    fetch: FetchSection = Field(default_factory=FetchSection, description="抓取默认参数")
    ratelimit: RateLimitSection = Field(default_factory=RateLimitSection, description="本地限速")
    cache: CacheSection = Field(default_factory=CacheSection, description="结果缓存")
    pages: PagesSection = Field(default_factory=PagesSection, description="网页正文磁盘缓存")
    logging: LoggingSection = Field(default_factory=LoggingSection, description="日志")
    commands: CommandsSection = Field(default_factory=CommandsSection, description="命令")


__all__ = [
    "ApiSection",
    "CacheSection",
    "CommandsSection",
    "DeepSeekSearchConfig",
    "FetchSection",
    "LoggingSection",
    "PagesSection",
    "PluginInfoSection",
    "RateLimitSection",
    "SearchSection",
]

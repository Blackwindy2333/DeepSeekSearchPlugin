"""MaiBot 插件入口：DeepSeek 联网搜索与网页抓取。

组件一览（全部挂在插件类上，逻辑下沉到 ``deepseek_search`` 子包）：

===================  ====================================================
组件                用途
===================  ====================================================
``search_web``       ``@Tool``：LLM 主动联网搜索
``fetch_webpage``    ``@Tool``：LLM 抓取指定网页正文
``/search``          ``@Command``：用户直接触发搜索
``/fetch``           ``@Command``：用户直接触发抓取
``search``           ``@API``：供其它插件调用（public）
``get_stats``        ``@API``：运行统计
``status``           ``@HomeCard``：首页状态卡片
===================  ====================================================

设计约定：

- 组件方法只做「参数兜底 → 调 service → 转成返回值」，不在组件里做网络和格式化；
- 生命周期只做「日志配置 + service 创建/热更新/释放」，失败不抛给 Host（避免整个插件被禁用）；
- 命令不自行发送消息，统一返回 ``(success, response, weight)`` 交给 Host。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# ── 导入引导 ───────────────────────────────────────────────
# MaiBot 的 Runner 会用 spec_from_file_location(..., submodule_search_locations=[插件目录])
# 加载 plugin.py，但 sys.path 中并没有插件目录本身；这里补上，保证子包以绝对名导入，
# 单实例、可被 pytest 直接 import。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:  # pragma: no cover - 正常只执行一次
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import (  # noqa: E402
    API,
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    HomeCard,
    MaiBotPlugin,
    Tool,
)
from maibot_sdk.types import ToolParamType, ToolParameterInfo  # noqa: E402

from deepseek_search.__version__ import PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION  # noqa: E402
from deepseek_search.config import DeepSeekSearchConfig  # noqa: E402
from deepseek_search.formatting import format_search_for_command  # noqa: E402
from deepseek_search.logging_utils import (  # noqa: E402
    LOGGER_NAMES,
    attach_file_handler,
    configure_logger,
    detach_file_handler,
    get_child,
)
from deepseek_search.services import DeepSeekSearchService  # noqa: E402
from deepseek_search.settings import settings as env_settings  # noqa: E402

#: 文件日志名
LOG_FILE_NAME = "dsp.log"
#: 首页卡片跳转的配置页
CONFIG_PAGE_LINK = f"/plugin-config?plugin={PLUGIN_ID}"
#: 命令用法提示
SEARCH_USAGE = "用法：/search 关键词（支持 site:/after:/before:/lang: 等检索语法）"
FETCH_USAGE = "用法：/fetch 网页链接（http/https）"
#: /fetch 命令回复的正文上限（聊天消息不适合贴长篇正文；工具调用不受此限制）
COMMAND_FETCH_MAX_CHARS = 1500


def _coerce_int(value: Any) -> int | None:
    """把模型传入的任意值转成 int，失败返回 None。"""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    """把模型传入的任意值转成 bool，无法判断返回 None。"""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on", "y"):
        return True
    if text in ("false", "0", "no", "off", "n"):
        return False
    return None


class DeepSeekSearchPlugin(MaiBotPlugin):
    """DeepSeek 搜索/抓取插件主类。"""

    config_model = DeepSeekSearchConfig

    def __init__(self) -> None:
        """初始化插件内部状态（此时上下文尚未注入）。"""

        super().__init__()
        self._logger: logging.Logger = logging.getLogger("maibot_sdk.plugin")
        self._service: DeepSeekSearchService | None = None
        self._log_file: Path | None = None

    # ── 生命周期 ────────────────────────────────────────────

    async def on_load(self) -> None:
        """插件加载：配置日志、创建 service、打印启动摘要。"""

        self._logger = self.ctx.logger
        config = self.config
        self._apply_logging(config)
        self._service = DeepSeekSearchService(
            config,
            logger=get_child(self._logger, "services"),
            env=env_settings,
        )
        self._logger.info(
            "插件加载完成 id=%s name=%s version=%s",
            PLUGIN_ID,
            PLUGIN_NAME,
            PLUGIN_VERSION,
        )
        snapshot = self._service.snapshot()
        self._logger.info(
            "API 配置 base_url=%s configured=%s source=%s",
            snapshot["api"]["base_url"],
            snapshot["api"]["configured"],
            snapshot["api"]["source"],
        )
        if not snapshot["api"]["configured"]:
            self._logger.warning("尚未配置 DeepSeek API Key，工具调用会返回配置提示（可在 WebUI 插件配置中填写）")
        self._logger.info(
            "能力开关 限速=%s 缓存=%s 命令=%s 文件日志=%s",
            config.ratelimit.enabled,
            config.cache.enabled,
            config.commands.enabled,
            config.logging.file_enabled,
        )

    async def on_unload(self) -> None:
        """插件卸载：释放连接池与文件日志句柄。"""

        if self._service is not None:
            try:
                await self._service.aclose()
            except Exception as exc:  # pragma: no cover - 卸载路径兜底
                self._logger.warning("释放 service 时出现异常（已忽略）: %s", exc)
            self._service = None
        if detach_file_handler(self._logger):
            self._logger.debug("已关闭文件日志")
        self._log_file = None
        self._logger.info("插件已卸载 id=%s", PLUGIN_ID)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新：刷新日志与 service，不做整体重启。"""

        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        config = self.config
        self._apply_logging(config)
        if self._service is None:
            self._service = DeepSeekSearchService(
                config,
                logger=get_child(self._logger, "services"),
                env=env_settings,
            )
            changes: dict[str, Any] = {"service": "created"}
        else:
            changes = await self._service.apply_config(config)
        self._logger.info("配置已热更新 scope=%s version=%s changes=%s", scope, version, changes or "无")

    # ── Tool 组件 ───────────────────────────────────────────

    @Tool(
        "search_web",
        brief_description="联网搜索获取实时网页信息",
        detailed_description=(
            "当需要最新信息、事实核查、新闻/行情/文档检索时调用。"
            "支持 site:/after:/before:/lang:/\"精确短语\" 等检索语法。"
            "返回标题、链接、摘要与关键点，务必保留链接作为出处；"
            "need_content=true 时附带网页正文（响应更大），仅在需要细节时使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索关键词或带语法的查询，最多 128 字符",
                required=True,
            ),
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="返回条数（1–50），留空使用插件默认值",
                required=False,
            ),
            ToolParameterInfo(
                name="need_content",
                param_type=ToolParamType.BOOLEAN,
                description="是否附带网页正文，留空使用插件默认值",
                required=False,
            ),
            ToolParameterInfo(
                name="ori_question",
                param_type=ToolParamType.STRING,
                description="用户的原始问题，用于结果重排，可选",
                required=False,
            ),
        ],
        core_tool=True,
    )
    async def search_web(
        self,
        query: str = "",
        count: int | None = None,
        need_content: bool | None = None,
        ori_question: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """LLM 工具：联网搜索。"""

        service = self._require_service("search_web")
        outcome = await service.search(
            query,
            count=_coerce_int(count),
            need_content=_coerce_bool(need_content),
            ori_question=str(ori_question or ""),
        )
        return outcome.to_dict()

    @Tool(
        "fetch_webpage",
        brief_description="抓取指定网页的正文内容",
        detailed_description=(
            "当已知具体网址、需要阅读全文细节时调用（例如搜索结果里最相关的链接）。"
            "只支持 http/https 公网链接；返回标题与 Markdown 正文，正文过长会截断。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要抓取的网页链接（http/https）",
                required=True,
            ),
            ToolParameterInfo(
                name="max_chars",
                param_type=ToolParamType.INTEGER,
                description="返回正文的最大字符数，留空使用插件默认值",
                required=False,
            ),
        ],
        core_tool=True,
    )
    async def fetch_webpage(
        self,
        url: str = "",
        max_chars: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """LLM 工具：抓取网页正文。"""

        service = self._require_service("fetch_webpage")
        outcome = await service.fetch(url, max_chars=_coerce_int(max_chars))
        return outcome.to_dict()

    # ── Command 组件 ────────────────────────────────────────

    @Command(
        "search",
        description="联网搜索（/search 关键词）",
        pattern=r"^(?:/search|/搜索)(?:\s+(?P<query>.+))?$",
    )
    async def cmd_search(self, **kwargs: Any) -> tuple[bool, str, int]:
        """命令：/search 关键词。"""

        config = self.config
        if not config.commands.enabled:
            return False, "搜索命令已被管理员禁用", 0
        groups = kwargs.get("matched_groups") or {}
        query = str(groups.get("query") or "").strip() if isinstance(groups, dict) else ""
        if not query:
            return False, SEARCH_USAGE, 0
        service = self._require_service("cmd_search")
        outcome = await service.search(query)
        if not outcome.success or outcome.result is None:
            return False, outcome.content, 0
        text = format_search_for_command(
            outcome.result,
            max_results=max(1, int(config.commands.max_results_in_reply)),
        )
        return True, text, 0

    @Command(
        "fetch",
        description="抓取网页正文（/fetch 链接）",
        pattern=r"^(?:/fetch|/抓取)(?:\s+(?P<url>\S+))?$",
    )
    async def cmd_fetch(self, **kwargs: Any) -> tuple[bool, str, int]:
        """命令：/fetch 链接。"""

        config = self.config
        if not config.commands.enabled:
            return False, "抓取命令已被管理员禁用", 0
        groups = kwargs.get("matched_groups") or {}
        url = str(groups.get("url") or "").strip() if isinstance(groups, dict) else ""
        if not url:
            return False, FETCH_USAGE, 0
        service = self._require_service("cmd_fetch")
        outcome = await service.fetch(
            url,
            max_chars=min(int(config.fetch.max_content_chars), COMMAND_FETCH_MAX_CHARS),
        )
        return bool(outcome.success), outcome.content, 0

    # ── API 组件（插件间 RPC） ──────────────────────────────

    @API(
        "search",
        description="供其它插件调用的联网搜索接口，返回结构化结果",
        version="1",
        public=True,
    )
    async def api_search(
        self,
        query: str = "",
        count: int | None = None,
        need_content: bool | None = None,
        ori_question: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """插件间 API：搜索并返回结构化数据。"""

        service = self._require_service("api_search")
        outcome = await service.search(
            query,
            count=_coerce_int(count),
            need_content=_coerce_bool(need_content),
            ori_question=str(ori_question or ""),
        )
        payload = outcome.to_dict()
        if outcome.result is not None:
            payload["results"] = [
                {
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "summary": item.summary,
                    "site_name": item.site_name,
                    "date": item.date,
                    "rerank_score": item.rerank_score,
                    "authority": item.authority,
                    "content": item.content,
                }
                for item in outcome.result.items
            ]
        return payload

    @API(
        "get_stats",
        description="返回插件运行状态与统计（搜索/抓取次数、缓存命中率、限速状态）",
        version="1",
        public=True,
    )
    async def api_get_stats(self, **kwargs: Any) -> dict[str, Any]:
        """插件间 API：运行统计。"""

        service = self._service
        if service is None:
            return {"ok": False, "error": "插件尚未完成加载", "plugin_id": PLUGIN_ID}
        payload = {"ok": True, "plugin_id": PLUGIN_ID, "version": PLUGIN_VERSION}
        payload.update(service.snapshot())
        return payload

    # ── HomeCard ────────────────────────────────────────────

    @HomeCard(
        "status",
        "DeepSeek 联网搜索",
        content=[
            {
                "type": "markdown",
                "content": (
                    "**运行中**。LLM 可调用工具 `search_web` 与 `fetch_webpage`；"
                    "用户可用命令 `/search 关键词` 与 `/fetch 链接`。"
                ),
            },
            {
                "type": "key_value",
                "entries": {
                    "工具": "search_web / fetch_webpage",
                    "命令": "/search、/fetch",
                    "缓存": "TTL + LRU（可在配置中关闭）",
                },
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "插件配置", "url": CONFIG_PAGE_LINK},
                    {
                        "label": "DeepSeek 文档",
                        "url": "https://api-docs.deepseek.com/zh-cn/guides/search/",
                    },
                ],
            },
        ],
        description="DeepSeek 搜索/抓取插件的运行状态与入口",
        link_url=CONFIG_PAGE_LINK,
        link_label="打开配置",
        icon="search",
        width="medium",
        order=100,
    )
    async def home_status(self, **kwargs: Any) -> dict[str, Any]:
        """首页卡片占位标记（Host 读取装饰器元数据，不调用本方法）。"""

        return {"ok": True}

    # ── 内部辅助 ────────────────────────────────────────────

    def _require_service(self, action: str) -> DeepSeekSearchService:
        """返回 service；未初始化时抛出一句可读的错误。

        Args:
            action: 触发动作（用于日志）。

        Returns:
            DeepSeekSearchService: 已初始化的 service。

        Raises:
            RuntimeError: 插件尚未完成加载。
        """

        if self._service is None:
            self._logger.error("插件尚未完成加载，拒绝执行 %s", action)
            raise RuntimeError("插件尚未完成加载，请稍后再试")
        return self._service

    def _data_dir(self) -> Path:
        """返回插件数据目录（Host 未提供时退化为相对路径）。"""

        try:
            paths = getattr(self.ctx, "paths", None)
            data_dir = getattr(paths, "data_dir", "") if paths is not None else ""
        except RuntimeError:  # pragma: no cover - 上下文缺失
            data_dir = ""
        if data_dir:
            return Path(str(data_dir))
        return Path("data") / "plugins" / PLUGIN_ID

    def _apply_logging(self, config: DeepSeekSearchConfig) -> None:
        """按配置（含环境变量覆盖）配置日志器与可选文件输出。

        Args:
            config: 当前配置实例。
        """

        level = env_settings.log_level or config.logging.level
        configure_logger(self._logger, level=level, refresh_child_names=LOGGER_NAMES.values())

        file_enabled = config.logging.file_enabled
        if env_settings.log_file_enabled is not None:
            file_enabled = env_settings.log_file_enabled
        if not file_enabled:
            detach_file_handler(self._logger)
            self._log_file = None
            return

        log_dir = Path(env_settings.log_dir) if env_settings.log_dir else self._data_dir() / "logs"
        self._log_file = attach_file_handler(
            self._logger,
            log_dir=log_dir,
            filename=LOG_FILE_NAME,
            level=level,
            max_bytes=int(config.logging.file_max_bytes),
            backup_count=int(config.logging.file_backup_count),
        )
        if self._log_file is not None:
            self._logger.info("文件日志已启用 path=%s level=%s", self._log_file, level)


def create_plugin() -> DeepSeekSearchPlugin:
    """MaiBot 插件工厂函数（Runner 通过它实例化插件）。

    Returns:
        DeepSeekSearchPlugin: 插件实例。
    """

    return DeepSeekSearchPlugin()


__all__ = ["DeepSeekSearchPlugin", "create_plugin"]

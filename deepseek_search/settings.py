"""环境变量覆盖（部署与本地联调用）。

MaiBot 插件正式配置来自 WebUI 生成的 ``config.toml``，本模块只提供一层**可选**覆盖：
不在配置里写死 Key、代理、数据目录，便于 CI/容器/多环境部署。

约定：环境变量优先级高于 ``config.toml``，但只覆盖「部署相关」字段，
搜索条数、缓存 TTL 这类业务策略仍以插件配置为准。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: 环境变量前缀
ENV_PREFIX = "MAIBOT_DSP"

#: 允许的日志级别
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _read_str(name: str, default: str = "") -> str:
    """读取字符串环境变量并去空白。"""

    return str(os.environ.get(name, default) or "").strip()


def _read_float(name: str) -> float | None:
    """读取浮点环境变量，缺省或非法返回 None。"""

    raw = _read_str(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _read_bool(name: str) -> bool | None:
    """读取布尔环境变量，缺省或无法识别返回 None。"""

    raw = _read_str(name).lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return None


def mask_secret(value: str, keep_tail: int = 4) -> str:
    """把密钥脱敏成 ``sk-***abcd`` 形式，供日志与报错使用。

    Args:
        value: 原始密钥。
        keep_tail: 保留的末尾字符数。

    Returns:
        str: 脱敏后的字符串；空值返回空串。
    """

    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= keep_tail:
        return "***"
    prefix = "sk-" if secret.startswith("sk-") else ""
    return f"{prefix}***{secret[-keep_tail:]}"


@dataclass(frozen=True)
class Settings:
    """运行期设置（环境变量合并结果）。"""

    #: 运行环境标识，仅用于日志
    environment: str = "prod"
    #: 覆盖插件配置中的 api.api_key
    api_key: str = ""
    #: 覆盖插件配置中的 api.base_url
    base_url: str = ""
    #: 覆盖插件配置中的 api.proxy
    proxy: str = ""
    #: 覆盖 logging.level
    log_level: str = ""
    #: 覆盖 logging.file_enabled
    log_file_enabled: bool | None = None
    #: 覆盖日志目录（默认用 ctx.paths.data_dir/logs）
    log_dir: str = ""
    #: 覆盖 HTTP 总超时
    http_timeout: float | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造设置。"""

        log_level = _read_str(f"{ENV_PREFIX}_LOG_LEVEL").upper()
        if log_level not in LOG_LEVELS:
            log_level = ""
        return cls(
            environment=_read_str(f"{ENV_PREFIX}_ENV", "prod") or "prod",
            api_key=_read_str(f"{ENV_PREFIX}_API_KEY"),
            base_url=_read_str(f"{ENV_PREFIX}_BASE_URL"),
            proxy=_read_str(f"{ENV_PREFIX}_PROXY"),
            log_level=log_level,
            log_file_enabled=_read_bool(f"{ENV_PREFIX}_LOG_FILE"),
            log_dir=_read_str(f"{ENV_PREFIX}_LOG_DIR"),
            http_timeout=_read_float(f"{ENV_PREFIX}_HTTP_TIMEOUT"),
        )

    def masked_api_key(self) -> str:
        """返回脱敏后的 API Key。"""

        return mask_secret(self.api_key)

    def describe(self) -> dict[str, str]:
        """返回可安全写入日志的环境摘要。"""

        return {
            "environment": self.environment,
            "api_key": self.masked_api_key() or "<未设置>",
            "base_url": self.base_url or "<config>",
            "proxy": self.proxy or "<未设置>",
            "log_level": self.log_level or "<config>",
            "http_timeout": "" if self.http_timeout is None else str(self.http_timeout),
        }


#: 模块级单例（首次导入时读取一次环境变量）
settings: Settings = Settings.from_env()


__all__ = ["ENV_PREFIX", "LOG_LEVELS", "Settings", "mask_secret", "settings"]

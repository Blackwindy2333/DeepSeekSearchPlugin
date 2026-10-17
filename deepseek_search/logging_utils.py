"""日志基础设施：密钥脱敏 + 可热更新的日志器配置。

约定：

- 基日志器由 Host 注入（``self.ctx.logger``，名称形如 ``plugin.<plugin_id>``），
  本模块只在它下面挂子日志器（``dsp.client`` / ``dsp.services`` …），**不新建独立 logger 树**，
  这样日志仍然汇入 MaiBot 的统一日志通道；
- ``SecretMaskFilter`` 在**渲染后**的文本上做脱敏，防止 Key 通过 ``%s`` 参数或异常信息泄漏；
- 文件输出可选（``logging.file_enabled``），路径在插件 data 目录下，``on_unload`` 时移除并关闭句柄。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Iterable

from .settings import mask_secret

#: 文件日志的格式（控制台沿用 Host 的格式，不覆盖）
FILE_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
#: 文件日志时间格式
FILE_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 匹配 sk- 开头的 API Key
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
#: 匹配 Authorization 头里的 Bearer token
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9_\.\-]{4,})", re.IGNORECASE)

#: 子日志器后缀（集中声明，避免各处硬编码字符串）
LOGGER_NAMES: dict[str, str] = {
    "client": "dsp.client",
    "services": "dsp.services",
    "cache": "dsp.cache",
    "ratelimit": "dsp.ratelimit",
    "tools": "dsp.tools",
    "commands": "dsp.commands",
    "api": "dsp.api",
    "plugin": "dsp.plugin",
}


def mask_text(text: str) -> str:
    """把文本中出现的 API Key 与 Bearer token 脱敏。

    Args:
        text: 任意日志文本。

    Returns:
        str: 脱敏后的文本。
    """

    masked = _API_KEY_RE.sub(lambda match: mask_secret(match.group(0)), text)
    return _BEARER_RE.sub(lambda match: f"{match.group(1)}{mask_secret(match.group(2))}", masked)


class SecretMaskFilter(logging.Filter):
    """日志过滤器：对消息与参数中的密钥做脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - 覆盖标准接口名
        """就地脱敏 ``record``，永远返回 True（不丢弃日志）。

        Args:
            record: 待输出的日志记录。

        Returns:
            bool: 恒为 True。
        """

        try:
            if isinstance(record.msg, str):
                record.msg = mask_text(record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(mask_text(str(arg)) if isinstance(arg, str) else arg for arg in record.args)
                elif isinstance(record.args, dict):
                    record.args = {
                        key: mask_text(str(value)) if isinstance(value, str) else value
                        for key, value in record.args.items()
                    }
            if record.exc_info and record.exc_text is None:
                # 异常文本在格式化时才生成，这里先记录一个可读的 traceback 文本并脱敏
                record.exc_text = mask_text(logging.Formatter().formatException(record.exc_info))
        except Exception:  # pragma: no cover - 过滤器自身绝不能打断日志
            return True
        return True


class _LevelHandler(logging.Handler):
    """占位 handler，用于保证「仅设置级别」时日志器仍然有输出通道。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 不会真正使用
        """什么都不做。"""


def ensure_mask_filter(logger: logging.Logger) -> None:
    """确保日志器（及其父链）挂上了脱敏过滤器，重复调用安全。

    Args:
        logger: 目标日志器。
    """

    current: logging.Logger | None = logger
    while current is not None:
        if not any(isinstance(item, SecretMaskFilter) for item in current.filters):
            current.addFilter(SecretMaskFilter())
        current = current.parent


def configure_logger(
    base_logger: logging.Logger,
    *,
    level: str = "INFO",
    refresh_child_names: Iterable[str] = (),
) -> logging.Logger:
    """设置基日志器级别并给子日志器挂脱敏过滤器。

    Args:
        base_logger: Host 注入的基日志器。
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）。
        refresh_child_names: 需要一并刷新过滤器的子日志器名称。

    Returns:
        logging.Logger: 传入的基日志器。
    """

    normalized = str(level or "INFO").upper()
    base_logger.setLevel(getattr(logging, normalized, logging.INFO))
    ensure_mask_filter(base_logger)
    for name in refresh_child_names:
        child = logging.getLogger(name)
        ensure_mask_filter(child)
    return base_logger


def attach_file_handler(
    base_logger: logging.Logger,
    *,
    log_dir: str | Path,
    filename: str = "dsp.log",
    level: str = "INFO",
    max_bytes: int = 2097152,
    backup_count: int = 3,
) -> Path | None:
    """给基日志器挂滚动文件 handler（已存在则复用）。

    Args:
        base_logger: Host 注入的基日志器。
        log_dir: 日志目录（通常是 ``ctx.paths.data_dir / "logs"``）。
        filename: 日志文件名。
        level: 文件日志级别。
        max_bytes: 单文件大小上限。
        backup_count: 备份文件个数。

    Returns:
        Path | None: 日志文件路径；创建失败返回 None（并记录一条 warning）。
    """

    marker = "_dsp_file_handler"
    existing = getattr(base_logger, marker, None)
    if isinstance(existing, logging.Handler):
        existing.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))
        base_path = getattr(existing, "baseFilename", "")
        return Path(base_path) if base_path else None

    try:
        target_dir = Path(log_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        log_path = target_dir / filename
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max(65536, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))
        handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, FILE_LOG_DATE_FORMAT))
        handler.addFilter(SecretMaskFilter())
        base_logger.addHandler(handler)
        setattr(base_logger, marker, handler)
        return log_path
    except OSError as exc:  # 磁盘/权限问题不应影响插件主流程
        base_logger.warning("无法创建日志文件，已退化为仅控制台输出: %s", exc)
        return None


def detach_file_handler(base_logger: logging.Logger) -> bool:
    """移除并关闭文件 handler（``on_unload`` 用）。

    Args:
        base_logger: 目标日志器。

    Returns:
        bool: 是否移除了 handler。
    """

    marker = "_dsp_file_handler"
    handler = getattr(base_logger, marker, None)
    if not isinstance(handler, logging.Handler):
        return False
    try:
        base_logger.removeHandler(handler)
        handler.close()
    finally:
        try:
            delattr(base_logger, marker)
        except AttributeError:
            pass
    return True


def get_child(base_logger: logging.Logger, key: str) -> logging.Logger:
    """按 ``LOGGER_NAMES`` 取子日志器。

    Args:
        base_logger: Host 注入的基日志器。
        key: ``LOGGER_NAMES`` 中的键。

    Returns:
        logging.Logger: 子日志器（名称基于基日志器）。
    """

    suffix = LOGGER_NAMES.get(key, key)
    return base_logger.getChild(suffix)


__all__ = [
    "FILE_LOG_DATE_FORMAT",
    "FILE_LOG_FORMAT",
    "LOGGER_NAMES",
    "SecretMaskFilter",
    "attach_file_handler",
    "configure_logger",
    "detach_file_handler",
    "ensure_mask_filter",
    "get_child",
    "mask_text",
]

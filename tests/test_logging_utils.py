"""日志基础设施：密钥脱敏、级别配置、滚动文件。"""

from __future__ import annotations

import logging

from deepseek_search.logging_utils import (
    SecretMaskFilter,
    attach_file_handler,
    configure_logger,
    detach_file_handler,
    get_child,
    mask_text,
)
from deepseek_search.settings import mask_secret


def unique_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.INFO)
    return logger


def test_mask_secret_keeps_last_four_chars():
    assert mask_secret("sk-1234567890abcdef") == "sk-***cdef"
    assert mask_secret("short") == "***hort"
    assert mask_secret("abcd") == "***"
    assert mask_secret("") == ""


def test_mask_text_masks_api_keys_and_bearer_tokens():
    text = "api_key=sk-abcdefgh Authorization: Bearer abcdefghij"
    masked = mask_text(text)
    assert "sk-abcdefgh" not in masked
    assert "Bearer abcdefghij" not in masked
    assert "sk-***efgh" in masked
    assert "Bearer ***ghij" in masked


def test_secret_filter_masks_message_and_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key=%s",
        args=("sk-abcdefgh",),
        exc_info=None,
    )
    assert SecretMaskFilter().filter(record) is True
    assert record.args == ("sk-***efgh",)

    direct = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Bearer abcdefghij",
        args=(),
        exc_info=None,
    )
    assert SecretMaskFilter().filter(direct) is True
    assert direct.msg == "Bearer ***ghij"


def test_configure_logger_sets_level_and_is_idempotent():
    logger = unique_logger("plugin.test.configure")
    configure_logger(logger, level="DEBUG", refresh_child_names=["dsp.test.child"])
    assert logger.level == logging.DEBUG
    configure_logger(logger, level="ERROR", refresh_child_names=["dsp.test.child"])
    assert logger.level == logging.ERROR
    assert sum(isinstance(item, SecretMaskFilter) for item in logger.filters) == 1
    assert sum(isinstance(item, SecretMaskFilter) for item in logging.getLogger("plugin").filters) <= 1


def test_get_child_uses_documented_suffix():
    logger = unique_logger("plugin.test.children")
    assert get_child(logger, "client").name == "plugin.test.children.dsp.client"
    assert get_child(logger, "services").name == "plugin.test.children.dsp.services"


def test_file_handler_writes_masked_rolling_log(tmp_path):
    logger = unique_logger("plugin.test.file")
    log_path = attach_file_handler(logger, log_dir=tmp_path / "logs", filename="dsp.log", level="INFO")
    assert log_path is not None
    assert log_path.parent.is_dir()

    logger.info("api_key=%s", "sk-abcdefgh")
    assert detach_file_handler(logger) is True
    content = log_path.read_text(encoding="utf-8")
    assert "sk-***efgh" in content
    assert "sk-abcdefgh" not in content


def test_attach_file_handler_reuses_existing_handler(tmp_path):
    logger = unique_logger("plugin.test.reuse")
    first = attach_file_handler(logger, log_dir=tmp_path / "logs")
    second = attach_file_handler(logger, log_dir=tmp_path / "logs")
    assert first == second
    assert detach_file_handler(logger) is True
    assert detach_file_handler(logger) is False


def test_attach_file_handler_survives_unwritable_directory(tmp_path):
    logger = unique_logger("plugin.test.unwritable")
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert attach_file_handler(logger, log_dir=blocker / "logs") is None
    assert detach_file_handler(logger) is False

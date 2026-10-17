"""DeepSeek 联网搜索与网页抓取插件（MaiBot）。

子模块按职责拆分，便于后续替换或扩展：

- ``config``        插件配置模型与默认配置节
- ``settings``      环境变量覆盖（部署 / 本地联调）
- ``errors``        统一异常体系与用户可读文案
- ``models``        数据模型（搜索结果、抓取结果、统计）
- ``logging_utils`` 日志器、脱敏过滤器、文件输出
- ``ratelimit``     令牌桶限速
- ``cache``         TTL + LRU 结果缓存
- ``http``          HTTP 异常类型（客户端实现见 ``client``）
- ``client``        DeepSeek Search / Fetch HTTP 客户端
- ``formatting``    将结果格式化为给 LLM / 用户看的文本
- ``services``      业务编排（搜索、抓取、缓存、限速、统计）
- ``metrics``       运行统计
"""

from __future__ import annotations

from .__version__ import PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION

__all__ = [
    "PLUGIN_ID",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
]

__version__ = PLUGIN_VERSION

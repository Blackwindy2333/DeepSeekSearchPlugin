"""数据模型：把官方 API 的原始 JSON 转成带类型的对象，供格式化与业务层使用。

设计要点：

- **宽容解析**：官方字段可能缺失（``site_name``/``date``/``site_icon`` 允许为 null），
  字段类型也可能出现 ``null`` 或数字字符串，这里统一兜底，解析失败不影响其它结果项；
- **保持原始顺序**：``results`` 已按相关性排序，不在本地重排；
- **不在这里截断文本**：截断属于展示策略，放到 ``formatting`` 层，避免缓存里存裁剪过的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _as_str(value: Any, default: str = "") -> str:
    """把任意值转成字符串，``None`` 转成默认值。"""

    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    """把任意值转成浮点数，失败返回默认值。"""

    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _as_int(value: Any, default: int = 0) -> int:
    """把任意值转成整数，失败返回默认值。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


@dataclass(frozen=True, slots=True)
class SearchItem:
    """单条搜索结果。"""

    title: str
    url: str
    snippet: str = ""
    summary: str = ""
    content: str = ""
    site_name: str = ""
    site_icon: str = ""
    date: str = ""
    rerank_score: float = 0.0
    authority: int = -1

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SearchItem":
        """从官方响应中的单个 ``results[]`` 元素构造。

        Args:
            payload: 原始结果字典。

        Returns:
            SearchItem: 归一化后的结果项；缺失字段使用空串/默认值。
        """

        if not isinstance(payload, Mapping):
            raise ValueError("搜索结果项不是对象")
        return cls(
            title=_as_str(payload.get("title")).strip(),
            url=_as_str(payload.get("url")).strip(),
            snippet=_as_str(payload.get("snippet")).strip(),
            summary=_as_str(payload.get("summary")).strip(),
            content=_as_str(payload.get("content")),
            site_name=_as_str(payload.get("site_name")).strip(),
            site_icon=_as_str(payload.get("site_icon")).strip(),
            date=_as_str(payload.get("date")).strip(),
            rerank_score=_as_float(payload.get("rerank_score")),
            authority=_as_int(payload.get("authority"), default=-1),
        )

    @property
    def is_high_quality(self) -> bool:
        """相关性是否达到「优质」阈值（``rerank_score >= 1.5``）。"""

        return self.rerank_score >= 1.5

    @property
    def is_authoritative(self) -> bool:
        """站点权威度是否达到「极权威」阈值（``authority >= 3``）。"""

        return self.authority >= 3

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典（缓存与 API 返回用）。"""

        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "summary": self.summary,
            "content": self.content,
            "site_name": self.site_name,
            "site_icon": self.site_icon,
            "date": self.date,
            "rerank_score": self.rerank_score,
            "authority": self.authority,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """一次搜索的结果集。"""

    query: str
    items: tuple[SearchItem, ...] = field(default_factory=tuple)
    requested_count: int = 0

    @classmethod
    def from_payload(cls, query: str, payload: Mapping[str, Any], *, requested_count: int = 0) -> "SearchResult":
        """从官方 ``/v1/tools/search`` 响应构造。

        Args:
            query: 本次查询词。
            payload: 响应 JSON。
            requested_count: 请求的条数（用于日志与统计）。

        Returns:
            SearchResult: 结果集；无结果时 ``items`` 为空。

        Raises:
            ValueError: 响应结构不符合预期（缺少 ``results`` 数组）。
        """

        raw_results = payload.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise ValueError("响应中的 results 不是数组")

        items: list[SearchItem] = []
        for raw in raw_results:
            try:
                items.append(SearchItem.from_payload(raw))
            except (TypeError, ValueError):
                # 单条脏数据不影响整体结果，业务层会记录条数差异
                continue
        return cls(query=query, items=tuple(items), requested_count=requested_count)

    @property
    def is_empty(self) -> bool:
        """是否没有搜到任何结果。"""

        return not self.items

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""

        return {
            "query": self.query,
            "requested_count": self.requested_count,
            "results": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次网页抓取的结果。"""

    url: str
    title: str = ""
    content: str = ""
    truncated: bool = False

    @classmethod
    def from_payload(cls, url: str, payload: Mapping[str, Any]) -> "FetchResult":
        """从官方 ``/v1/tools/fetch`` 响应构造。

        Args:
            url: 被抓取的链接。
            payload: 响应 JSON。

        Returns:
            FetchResult: 抓取结果。

        Raises:
            ValueError: 响应中既没有 title 也没有 content。
        """

        title = _as_str(payload.get("title")).strip()
        content = _as_str(payload.get("content"))
        if not title and not content:
            raise ValueError("响应中缺少 title 与 content")
        return cls(url=url, title=title, content=content)

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""

        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "truncated": self.truncated,
        }


__all__ = ["FetchResult", "SearchItem", "SearchResult"]

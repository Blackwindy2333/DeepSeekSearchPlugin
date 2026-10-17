"""文本格式化：把结构化结果变成 LLM / 用户可读的中文文本。

原则：

- **出处优先**：每条结果保留标题与 URL，方便模型引用、用户点开核对；
- **截断集中**：所有长度限制都来自配置并提供默认值，测试可精确断言；
- **不拼接 Markdown 标题层级**：命令回复会再包一层，避免嵌套混乱；
- **空结果明确**：搜不到时返回「没找到」而不是空字符串，避免模型误以为调用失败。
"""

from __future__ import annotations

from .errors import DeepSeekSearchError
from .models import FetchResult, SearchItem, SearchResult

#: 权威度说明（仅当配置开启提示时使用）
_AUTHORITY_LABELS: dict[int, str] = {
    4: "极权威",
    3: "高权威",
    2: "中等",
    1: "较低",
    0: "低",
    -1: "未知",
}


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    """按字符截断文本（不超过 ``limit``）。

    Args:
        text: 原文。
        limit: 最大字符数，小于等于 0 返回空串。
        suffix: 截断后追加的后缀。

    Returns:
        str: 截断后的文本。
    """

    raw = str(text or "")
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return raw
    if limit <= len(suffix):
        return suffix[:limit]
    return raw[: limit - len(suffix)] + suffix


def _format_date(date: str) -> str:
    """把 RFC3339 时间压缩成 ``YYYY-MM-DD``。"""

    value = str(date or "").strip()
    if not value:
        return ""
    return value[:10]


def _authority_hint(item: SearchItem) -> str:
    """生成权威度提示片段。"""

    if item.authority < 0:
        return "权威度未知"
    label = _AUTHORITY_LABELS.get(item.authority, f"级别 {item.authority}")
    return f"权威度 {item.authority}/4（{label}）"


def format_search_result(
    result: SearchResult,
    *,
    max_results: int = 8,
    snippet_max_chars: int = 200,
    summary_max_chars: int = 500,
    content_max_chars: int = 1200,
    show_authority_hint: bool = True,
    include_tail_hint: bool = True,
) -> str:
    """把搜索结果格式化成给 LLM 阅读的中文文本。

    Args:
        result: 搜索结果集。
        max_results: 最多展示的结果条数。
        snippet_max_chars: 摘要片段截断长度。
        summary_max_chars: 结构化摘要截断长度。
        content_max_chars: 单条正文截断长度。
        show_authority_hint: 是否标注站点权威度。
        include_tail_hint: 是否追加「引用请保留链接」的提示。

    Returns:
        str: 可直接作为工具返回值 ``content`` 的文本。
    """

    if result.is_empty:
        return f'没有找到与「{result.query}」相关的结果。可以换个更具体的关键词，或加上 site: 限定站点。'

    shown = result.items[: max(1, int(max_results))]
    lines: list[str] = [f'找到 {len(result.items)} 条与「{result.query}」相关的结果（展示前 {len(shown)} 条）：']
    for index, item in enumerate(shown, start=1):
        source_bits: list[str] = []
        if item.site_name:
            source_bits.append(item.site_name)
        date_text = _format_date(item.date)
        if date_text:
            source_bits.append(date_text)
        if show_authority_hint:
            source_bits.append(_authority_hint(item))
        source = f"（{' · '.join(source_bits)}）" if source_bits else ""

        lines.append(f"{index}. {item.title or '（无标题）'}{source}")
        lines.append(f"   链接：{item.url}")
        if item.snippet:
            lines.append(f"   摘要：{truncate(item.snippet, snippet_max_chars)}")
        if item.summary:
            lines.append(f"   要点：{truncate(item.summary, summary_max_chars)}")
        if item.content:
            lines.append(f"   正文节选：{truncate(item.content, content_max_chars)}")
        lines.append("")

    if include_tail_hint:
        lines.append("以上结果由 DeepSeek 搜索 API 提供；内容可能已过时，引用时请保留链接。")
    return "\n".join(lines).strip()


def format_fetch_result(
    result: FetchResult,
    *,
    max_content_chars: int = 4000,
    include_title: bool = True,
) -> str:
    """把抓取结果格式化成给 LLM 阅读的中文文本。

    Args:
        result: 抓取结果。
        max_content_chars: 正文字符上限。
        include_title: 是否输出标题行。

    Returns:
        str: 格式化文本。
    """

    body = truncate(result.content, max_content_chars)
    lines: list[str] = []
    if include_title and result.title:
        lines.append(f"标题：{result.title}")
    lines.append(f"链接：{result.url}")
    if result.truncated or len(result.content) > max_content_chars:
        lines.append(f"正文（已截断，原文 {len(result.content)} 字符）：")
    else:
        lines.append("正文：")
    lines.append(body or "（未提取到正文）")
    return "\n".join(lines)


def format_search_for_command(
    result: SearchResult,
    *,
    max_results: int = 5,
    snippet_max_chars: int = 120,
) -> str:
    """把搜索结果格式化成命令回复（更紧凑，方便手机阅读）。

    Args:
        result: 搜索结果集。
        max_results: 最多展示条数。
        snippet_max_chars: 摘要片段长度。

    Returns:
        str: 命令回复文本。
    """

    if result.is_empty:
        return f'没有找到与「{result.query}」相关的结果。'

    shown = result.items[: max(1, int(max_results))]
    lines: list[str] = [f'「{result.query}」的搜索结果（{len(result.items)} 条，展示 {len(shown)} 条）：']
    for index, item in enumerate(shown, start=1):
        title = truncate(item.title or "（无标题）", 60)
        lines.append(f"{index}. {title}")
        lines.append(f"   {item.url}")
        if item.snippet:
            lines.append(f"   {truncate(item.snippet, snippet_max_chars)}")
    return "\n".join(lines)


def format_error(error: BaseException) -> str:
    """把异常转换成给用户/LLM 看的中文说明。

    Args:
        error: 任意异常。

    Returns:
        str: 中文说明；``DeepSeekSearchError`` 使用其 ``user_message``。
    """

    if isinstance(error, DeepSeekSearchError):
        return error.user_message
    return "搜索过程中出现未预期的错误，请稍后再试"


def format_status_lines(
    *,
    search_success: int,
    search_failed: int,
    fetch_success: int,
    fetch_failed: int,
    cache_hit_rate: float,
) -> str:
    """生成首页卡片用的多行状态摘要（Markdown）。"""

    return (
        f"- 搜索成功 {search_success} 次，失败 {search_failed} 次\n"
        f"- 抓取成功 {fetch_success} 次，失败 {fetch_failed} 次\n"
        f"- 缓存命中率 {cache_hit_rate:.0%}"
    )


__all__ = [
    "format_error",
    "format_fetch_result",
    "format_search_for_command",
    "format_search_result",
    "format_status_lines",
    "truncate",
]

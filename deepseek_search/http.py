"""URL 与 HTTP 辅助工具。

这里的 ``validate_url`` 是**客户端侧的安全闸门**：在把链接交给 DeepSeek Fetch 之前，
先挡掉明显不合法或指向内网的地址。它不能替代服务端的安全策略，但能减少无效请求与本地风险：

- 只允许 ``http`` / ``https``；
- 必须带主机名，长度不超过 8192（与官方限制一致）；
- 不允许携带用户名/密码（``http://user:pass@host``）；
- 主机是字面量 IP 时，拒绝私有/回环/链路本地/保留地址。
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

from .errors import UnsupportedUrlError

#: URL 最大长度（官方 fetch 限制）
MAX_URL_LENGTH = 8192
#: 允许的协议
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 明确禁止的主机名（本机回环）
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
#: 明确禁止的域名后缀（内网保留域）
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
#: 非规范 IPv4 字面量（十进制整数、十六进制、缩短形式），如 2130706433 / 0x7f.0.0.1 / 127.1
_NON_CANONICAL_IPV4 = re.compile(r"^(?:0[xX][0-9a-fA-F]+|\d+)(?:\.(?:0[xX][0-9a-fA-F]+|\d+)){0,3}\.?$")


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """尝试把主机名解析成字面量 IP。

    Args:
        host: 小写主机名。

    Returns:
        IP 对象；不是 IP 字面量（含十进制整数形式）时返回 ``None``。
    """

    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        # 某些写法把 IPv4 当作 32 位整数（如 2130706433 == 127.0.0.1）
        try:
            return ipaddress.IPv4Address(int(host))
        except (ValueError, ipaddress.AddressValueError):
            return None
    return None


def validate_url(url: str) -> str:
    """校验并规范化待抓取 URL。

    Args:
        url: 用户或模型给出的链接。

    Returns:
        str: 去掉首尾空白后的规范 URL。

    Raises:
        UnsupportedUrlError: 协议、长度、主机名或凭据不符合要求。
    """

    candidate = str(url or "").strip()
    if not candidate:
        raise UnsupportedUrlError("链接为空", user_message="请提供要抓取的网页链接")
    if len(candidate) > MAX_URL_LENGTH:
        raise UnsupportedUrlError("链接过长", user_message=f"链接长度超过 {MAX_URL_LENGTH} 字符，无法抓取")
    if any(ord(char) < 0x20 or char == "\x7f" for char in candidate):
        raise UnsupportedUrlError("链接包含控制字符", user_message="链接格式不正确")

    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError as exc:
        raise UnsupportedUrlError(f"链接无法解析: {exc}", user_message="链接格式不正确") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedUrlError(f"不支持的协议: {scheme!r}", user_message="只支持 http/https 链接")
    if not parsed.hostname:
        raise UnsupportedUrlError("链接缺少主机名", user_message="链接缺少主机名，无法抓取")
    if parsed.username or parsed.password:
        raise UnsupportedUrlError("链接包含用户名或密码", user_message="链接里不能包含用户名或密码")

    host = parsed.hostname.strip().lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsupportedUrlError(f"禁止抓取本机/内网地址: {host}", user_message="不支持抓取本机/内网地址")

    ip = _parse_ip_literal(host)
    if ip is not None:
        if not ip.is_global:
            raise UnsupportedUrlError(f"禁止抓取非公网地址: {host}", user_message="不支持抓取本机/内网地址")
    elif _NON_CANONICAL_IPV4.match(host):
        # 0x7f.0.0.1 / 127.1 / 2130706433 这类写法容易被解析器当作内网地址，一律拒绝
        raise UnsupportedUrlError(f"禁止抓取非规范 IP 地址: {host}", user_message="不支持抓取本机/内网地址")

    return candidate


def is_url_like(text: str) -> bool:
    """粗略判断一段文本是否像个链接（用于参数兜底）。

    Args:
        text: 待判断文本。

    Returns:
        bool: 以 http(s):// 开头即视为链接。
    """

    return str(text or "").strip().lower().startswith(("http://", "https://"))


__all__ = ["ALLOWED_SCHEMES", "MAX_URL_LENGTH", "is_url_like", "validate_url"]

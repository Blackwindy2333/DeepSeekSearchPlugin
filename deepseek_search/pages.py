"""网页正文磁盘缓存：按 URL 把抓取到的正文落盘，供长网页分段续读。

为什么需要它：``/v1/tools/fetch`` 返回的正文动辄数万字符，一次性塞进上下文会挤占
token 预算。插件把正文缓存到自己的 data 目录，之后可以按段读取；磁盘缓存同时承担
「进程重启后仍可续读」的职责，所以不能只放在内存里。

设计要点：

- **URL 即主键**：``sha256(url)[:32]`` 作为文件名，规避 Windows 非法字符与超长路径，
  URL 原文仍写在 JSON 里，排障时可直接查看；
- **原子写**：先写同目录临时文件再 ``os.replace``，进程崩溃不会留下半截 JSON；
- **容量双限**：条数 + 总字节数，超限按 mtime 从旧到新淘汰；但「刚写入的那条」永不
  淘汰，否则调用方拿到路径后立刻失效；
- **失败不抛**：所有 ``OSError`` / JSON 解析错误都被捕获并记日志，磁盘异常不能影响
  搜索、抓取主流程；
- **线程安全**：插件命令可能并发调用，所有公开方法持同一把 ``RLock``（可重入，
  ``put`` 内部调用 ``_enforce_limits`` 不会自锁）；
- **禁用即空转**：``root`` 为 ``None`` / 空串时不碰磁盘，读写方法安全 no-op。

日志事件名固定为 ``pages.hit`` / ``pages.save`` / ``pages.save_failed`` /
``pages.expired`` / ``pages.corrupt`` / ``pages.evict``，便于按关键字检索。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 未注入 logger 时使用的默认日志器名称（与 Host 注入的子日志器命名习惯一致）
_DEFAULT_LOGGER_NAME = "maibot_sdk.plugin.deepseek_search.pages"


def _as_str(value: object) -> str:
    """把任意值转成字符串，``None`` 与缺失字段转成空串。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_float(value: object, default: float = 0.0) -> float:
    """把任意值转成浮点数，非数字（含无法解析的字符串）返回默认值。"""

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


def _as_int(value: object, default: int = 0) -> int:
    """把任意值转成整数，转换失败返回默认值。"""

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


@dataclass(slots=True)
class PageRecord:
    """一条落盘的网页正文记录。"""

    url: str
    title: str = ""
    content: str = ""
    fetched_at: float = field(default_factory=time.time)

    @property
    def byte_size(self) -> int:
        """正文的 UTF-8 字节数（容量统计用）。"""

        return len(self.content.encode("utf-8"))

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的字典。"""

        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PageRecord":
        """从字典构造记录，字段缺失或类型异常时兜底。

        Args:
            payload: ``to_dict`` 产生的字典，允许字段缺失或类型不合法。

        Returns:
            PageRecord: 归一化后的记录；缺失字符串字段为空串，``fetched_at``
            非法时为 0.0。
        """

        if not isinstance(payload, dict):
            payload = {}
        return cls(
            url=_as_str(payload.get("url")),
            title=_as_str(payload.get("title")),
            content=_as_str(payload.get("content")),
            fetched_at=_as_float(payload.get("fetched_at")),
        )


class PageStore:
    """按 URL 落盘的网页正文缓存（条数 + 总字节数双限额）。

    缓存文件位于 ``root`` 目录，文件名是 URL 的 sha256 前缀（固定 32 个十六进制
    字符），因此同一 URL 覆盖写不会产生多余文件。所有方法都不抛异常：磁盘或内容
    异常时返回 ``None`` / ``False`` / ``0`` 并记日志。
    """

    def __init__(
        self,
        root: str | Path | None,
        *,
        ttl_seconds: float = 86400.0,
        max_entries: int = 64,
        max_total_bytes: int = 8 * 1024 * 1024,
        logger: logging.Logger | None = None,
    ) -> None:
        """初始化缓存。

        Args:
            root: 缓存目录；``None`` 或空串表示禁用（所有方法安全 no-op）。
            ttl_seconds: 有效期（秒），``0`` 表示不按时间过期；负数统一夹到 0。
            max_entries: 条数上限，小于 1 统一夹到 1。
            max_total_bytes: 总字节上限，负数统一夹到 0。
            logger: 外部注入的日志器，缺省使用 ``maibot_sdk.plugin.deepseek_search.pages``。
        """

        self._logger = logger or logging.getLogger(_DEFAULT_LOGGER_NAME)
        self._root: Path | None = Path(root) if root else None
        self._lock = threading.RLock()
        self._last_saved: Path | None = None
        # 先放合法默认值，再由 update_settings 统一 clamp 用户传入值（clamp 只有这一处实现）
        self._ttl_seconds = 0.0
        self._max_entries = 1
        self._max_total_bytes = 0
        self.update_settings(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )

    @property
    def enabled(self) -> bool:
        """是否启用磁盘缓存（``root`` 为空即禁用）。"""

        return self._root is not None

    @property
    def root(self) -> Path | None:
        """缓存目录；禁用时为 ``None``。"""

        return self._root

    def update_settings(
        self,
        *,
        ttl_seconds: float | None = None,
        max_entries: int | None = None,
        max_total_bytes: int | None = None,
    ) -> None:
        """热更新限额（线程安全，参数统一夹到合法区间）。

        Args:
            ttl_seconds: 新的有效期（秒），负数夹到 0。
            max_entries: 新的条数上限，小于 1 夹到 1。
            max_total_bytes: 新的总字节上限，负数夹到 0。
        """

        with self._lock:
            if ttl_seconds is not None:
                self._ttl_seconds = max(0.0, _as_float(ttl_seconds))
            if max_entries is not None:
                self._max_entries = max(1, _as_int(max_entries, default=1))
            if max_total_bytes is not None:
                self._max_total_bytes = max(0, _as_int(max_total_bytes, default=0))

    def get(self, url: str) -> PageRecord | None:
        """读取 URL 对应的缓存记录。

        Args:
            url: 网页地址。

        Returns:
            PageRecord | None: 命中且未过期时返回记录；未启用、不存在、损坏、
            过期或读失败时返回 ``None``（损坏/过期文件会被顺手删除）。
        """

        if not self.enabled or self._is_blank_url(url):
            return None
        with self._lock:
            path = self._path_for(url)
            record = self._load_record(path, url=url)
            if record is None:
                return None
            if self._ttl_seconds > 0:
                age = time.time() - record.fetched_at
                if age > self._ttl_seconds:
                    removed = self._safe_unlink(path)
                    self._logger.info(
                        "pages.expired url=%s path=%s age=%.1fs ttl=%.1fs removed=%s",
                        url,
                        path.name,
                        age,
                        self._ttl_seconds,
                        removed,
                    )
                    return None
            self._logger.debug("pages.hit url=%s chars=%d", url, len(record.content))
            return record

    def put(self, record: PageRecord) -> Path | None:
        """写入记录（先写临时文件再原子替换）。

        Args:
            record: 待写入的记录，``url`` 为空的记录会被拒绝。

        Returns:
            Path | None: 写入成功后最终文件路径；未启用、URL 为空或写失败返回
            ``None``（写失败会清掉临时文件并记 warning）。
        """

        if not self.enabled:
            return None
        if not isinstance(record, PageRecord):
            self._logger.warning(
                "pages.save_failed url=%s reason=not_a_page_record type=%s",
                getattr(record, "url", ""),
                type(record).__name__,
            )
            return None
        if self._is_blank_url(record.url):
            self._logger.warning("pages.save_failed url=%s reason=empty_url", record.url)
            return None

        with self._lock:
            path = self._path_for(record.url)
            tmp_path = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                text = json.dumps(record.to_dict(), ensure_ascii=False)
                tmp_path.write_text(text, encoding="utf-8")
                os.replace(tmp_path, path)
            except (OSError, TypeError, ValueError) as exc:
                self._safe_unlink(tmp_path)
                self._logger.warning("pages.save_failed url=%s error=%s", record.url, exc)
                return None

            self._last_saved = path
            self._logger.debug(
                "pages.save url=%s chars=%d bytes=%d",
                record.url,
                len(record.content),
                record.byte_size,
            )
            self._enforce_limits()
            return path

    def delete(self, url: str) -> bool:
        """删除 URL 对应的缓存文件。

        Args:
            url: 网页地址。

        Returns:
            bool: 是否真的删除了文件（不存在或未启用返回 ``False``，删除失败
            记 warning 后返回 ``False``）。
        """

        if not self.enabled or self._is_blank_url(url):
            return False
        with self._lock:
            return self._safe_unlink(self._path_for(url))

    def purge_expired(self) -> int:
        """扫描缓存目录，删除所有过期与损坏的文件。

        Returns:
            int: 实际删除的文件数量；未启用时返回 0。
        """

        if not self.enabled:
            return 0
        removed = 0
        with self._lock:
            root = self._root
            if root is None:  # 仅为类型收窄，enabled 已保证非空
                return 0
            for path in list(root.glob("*.json")):
                if not path.exists():
                    continue
                record = self._load_record(path, url=path.name)
                if record is None:
                    # 损坏文件在解析阶段已被删除；删除失败时文件仍在，不计入数量
                    if not path.exists():
                        removed += 1
                    continue
                if self._ttl_seconds <= 0:
                    continue
                age = time.time() - record.fetched_at
                if age > self._ttl_seconds and self._safe_unlink(path):
                    removed += 1
                    self._logger.info(
                        "pages.expired url=%s path=%s age=%.1fs ttl=%.1fs",
                        record.url,
                        path.name,
                        age,
                        self._ttl_seconds,
                    )
        return removed

    def stats(self) -> dict[str, int | None]:
        """返回缓存统计（条目数、总字节数与当前限额）。

        Returns:
            dict[str, int | None]: ``entries`` / ``total_bytes`` / ``max_entries`` /
            ``max_total_bytes``；未启用时前两项为 0。
        """

        entries = 0
        total_bytes = 0
        if self.enabled:
            with self._lock:
                root = self._root
                if root is not None:
                    for path in root.glob("*.json"):
                        try:
                            total_bytes += path.stat().st_size
                        except OSError as exc:
                            self._logger.warning("统计页面缓存失败 path=%s error=%s", path, exc)
                            continue
                        entries += 1
        with self._lock:
            return {
                "entries": entries,
                "total_bytes": total_bytes,
                "max_entries": self._max_entries,
                "max_total_bytes": self._max_total_bytes,
            }

    def _path_for(self, url: str) -> Path:
        """计算 URL 对应的缓存文件路径。"""

        root = self._root
        if root is None:
            raise RuntimeError("PageStore 未启用，无法计算缓存路径")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return root / f"{digest}.json"

    def _load_record(self, path: Path, *, url: str = "") -> PageRecord | None:
        """读取并解析缓存文件；不存在 / 损坏 / 读失败都返回 ``None``。"""

        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._logger.warning("读取页面缓存失败 url=%s path=%s error=%s", url, path, exc)
            return None

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            self._discard_corrupt(path, url=url, error=str(exc))
            return None
        if not isinstance(payload, dict):
            self._discard_corrupt(path, url=url, error=f"payload_type={type(payload).__name__}")
            return None
        return PageRecord.from_dict(payload)

    def _discard_corrupt(self, path: Path, *, url: str, error: str) -> None:
        """记录损坏日志并删除文件。"""

        self._logger.warning("pages.corrupt url=%s path=%s error=%s", url, path.name, error)
        self._safe_unlink(path)

    def _safe_unlink(self, path: Path) -> bool:
        """删除文件；返回是否真的删除（不存在或失败都返回 ``False``）。"""

        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._logger.warning("删除页面缓存失败 path=%s error=%s", path, exc)
            return False
        return True

    def _cleanup_temp_files(self) -> None:
        """清理崩溃残留的临时文件（``*.tmp``），失败只记日志。"""

        root = self._root
        if root is None:
            return
        for path in root.glob("*.tmp"):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._logger.warning("清理页面缓存临时文件失败 path=%s error=%s", path, exc)

    def _enforce_limits(self) -> None:
        """按 mtime 从旧到新淘汰，直到条数与总字节数回到限额内。

        刚写入的记录（``self._last_saved``）永不淘汰：如果它恰好是最旧的，跳过它
        继续删下一条；若只剩它一个则保留（允许临时超出字节上限，避免调用方刚拿到
        路径就失效）。
        """

        if not self.enabled:
            return
        with self._lock:
            root = self._root
            if root is None:  # 仅为类型收窄
                return
            self._cleanup_temp_files()

            entries: list[tuple[float, int, Path]] = []
            for path in root.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError as exc:
                    # 并发删除或权限异常：跳过，等下次淘汰/清扫再处理
                    self._logger.warning("统计页面缓存失败 path=%s error=%s", path, exc)
                    continue
                entries.append((stat.st_mtime, stat.st_size, path))
            entries.sort(key=lambda item: (item[0], item[2].name))

            total_bytes = sum(size for _, size, _ in entries)
            count = len(entries)
            for _, size, path in entries:
                if count <= self._max_entries and total_bytes <= self._max_total_bytes:
                    break
                if path == self._last_saved:
                    continue
                if not self._safe_unlink(path):
                    continue
                count -= 1
                total_bytes -= size
                reason = "entries" if count > self._max_entries else "total_bytes"
                self._logger.info(
                    "pages.evict path=%s entries=%d bytes=%d reason=%s",
                    path,
                    count,
                    total_bytes,
                    reason,
                )

    @staticmethod
    def _is_blank_url(url: object) -> bool:
        """URL 是否为空或只有空白字符。"""

        return not isinstance(url, str) or not url.strip()


__all__ = ["PageRecord", "PageStore"]

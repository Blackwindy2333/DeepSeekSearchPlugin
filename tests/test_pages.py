"""网页正文磁盘缓存（``deepseek_search.pages``）的单元测试。

覆盖：读写往返（含中文与 100KB 级大文本）、禁用模式、空 URL、TTL 过期、损坏
JSON、覆盖写、条数/字节数淘汰、``update_settings`` 夹紧、``stats`` 一致性、
``PageRecord`` 宽容解析、``delete`` 返回值、``purge_expired`` 与日志事件名。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_search.pages import PageRecord, PageStore

import logging
import os
import time

import pytest

from deepseek_search import pages


def _record(url: str, content: str = "正文", *, title: str = "") -> PageRecord:
    """构造一条带当前时间的记录。"""

    return PageRecord(url=url, title=title, content=content, fetched_at=time.time())


def test_put_get_roundtrip_with_chinese_and_large_content(tmp_path):
    """中文、换行、100KB 级正文可以原样写入并读回。"""

    store = PageStore(tmp_path / "pages")
    url = "https://example.com/article?query=中文&page=1"
    content = "长网页正文段落。\n" * 4096  # 每段 25 字节，共 102400 字节
    record = PageRecord(url=url, title="示例 标题", content=content, fetched_at=time.time())

    saved = store.put(record)
    assert saved is not None
    assert saved.exists()
    assert saved.parent == tmp_path / "pages"

    text = saved.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["url"] == url
    assert payload["title"] == "示例 标题"
    assert payload["content"] == content
    assert "长网页正文" in text  # ensure_ascii=False，文件里是原始中文

    loaded = store.get(url)
    assert loaded is not None
    assert loaded.url == url
    assert loaded.title == "示例 标题"
    assert loaded.content == content
    assert loaded.fetched_at == record.fetched_at
    assert loaded.byte_size == 102_400
    assert store.stats()["entries"] == 1


@pytest.mark.parametrize("root", [None, ""])
def test_disabled_store_is_safe_noop(root, tmp_path):
    """root 为 None/空串时所有读写都是安全 no-op。"""

    store = PageStore(root)
    assert store.enabled is False
    assert store.root is None

    assert store.put(PageRecord(url="https://example.com/x", content="正文")) is None
    assert store.get("https://example.com/x") is None
    assert store.delete("https://example.com/x") is False
    assert store.purge_expired() == 0

    stats = store.stats()
    assert stats["entries"] == 0
    assert stats["total_bytes"] == 0
    assert not any((tmp_path / "pages").glob("*"))


def test_put_rejects_blank_url(tmp_path, caplog):
    """url 为空的记录不允许写入，且不会创建目录/文件。"""

    store = PageStore(tmp_path / "pages", logger=logging.getLogger("tests.pages.blank"))
    caplog.set_level(logging.WARNING)

    assert store.put(PageRecord(url="", content="正文")) is None
    assert store.put(PageRecord(url="   ", content="正文")) is None
    assert store.get("") is None
    assert store.delete("") is False
    assert not (tmp_path / "pages").exists()
    assert "pages.save_failed" in caplog.text


def test_ttl_expiry_deletes_file(tmp_path, monkeypatch, caplog):
    """超过 TTL 后 get 返回 None 并删除文件。"""

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(pages.time, "time", lambda: clock["now"])
    store = PageStore(tmp_path / "pages", ttl_seconds=60.0, logger=logging.getLogger("tests.pages.ttl"))
    url = "https://example.com/ttl"

    saved = store.put(PageRecord(url=url, content="正文", fetched_at=clock["now"]))
    assert saved is not None
    assert store.get(url) is not None  # 未过期

    clock["now"] += 61.0
    caplog.set_level(logging.INFO)
    assert store.get(url) is None
    assert not saved.exists()
    assert "pages.expired" in caplog.text


def test_corrupt_json_is_deleted_and_returns_none(tmp_path, caplog):
    """JSON 损坏或顶层不是对象时按损坏处理：返回 None 并删除文件。"""

    store = PageStore(tmp_path / "pages", logger=logging.getLogger("tests.pages.corrupt"))
    caplog.set_level(logging.WARNING)
    url = "https://example.com/corrupt"

    saved = store.put(PageRecord(url=url, content="正文", fetched_at=time.time()))
    assert saved is not None
    saved.write_text("{this is not json", encoding="utf-8")
    assert store.get(url) is None
    assert not saved.exists()

    saved = store.put(PageRecord(url=url, content="正文", fetched_at=time.time()))
    assert saved is not None
    saved.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert store.get(url) is None
    assert not saved.exists()
    assert "pages.corrupt" in caplog.text


def test_overwrite_same_url_keeps_single_file(tmp_path):
    """覆盖写同一 URL 不产生额外文件，也不残留临时文件。"""

    store = PageStore(tmp_path / "pages")
    url = "https://example.com/same"

    first = store.put(PageRecord(url=url, content="第一版", fetched_at=time.time()))
    second = store.put(PageRecord(url=url, content="第二版", fetched_at=time.time()))
    assert first is not None and second is not None
    assert first == second

    directory = tmp_path / "pages"
    assert len(list(directory.glob("*.json"))) == 1
    assert list(directory.glob("*.tmp")) == []
    loaded = store.get(url)
    assert loaded is not None and loaded.content == "第二版"


def test_max_entries_evicts_oldest(tmp_path):
    """超过 max_entries 时先淘汰最旧的，最新写入的记录保留。"""

    store = PageStore(tmp_path / "pages", max_entries=2)
    base = 1_000_000.0  # 显式设置 mtime，避免依赖文件系统时间精度
    urls = [f"https://example.com/page-{index}" for index in range(5)]
    saved_paths: list[Path] = []
    for index, url in enumerate(urls):
        saved = store.put(PageRecord(url=url, content=f"内容{index}", fetched_at=time.time()))
        assert saved is not None
        os.utime(saved, (base + index, base + index))
        saved_paths.append(saved)

    files = list((tmp_path / "pages").glob("*.json"))
    assert len(files) == 2
    assert saved_paths[4].exists()
    assert saved_paths[3].exists()
    assert not saved_paths[0].exists()
    assert not saved_paths[1].exists()
    assert not saved_paths[2].exists()
    assert store.get(urls[4]) is not None
    assert store.get(urls[0]) is None


def test_max_total_bytes_evicts_until_under_budget(tmp_path):
    """总字节超限时按旧到新淘汰，保留最新记录且总字节回到上限内。"""

    budget = 15_000
    store = PageStore(tmp_path / "pages", max_total_bytes=budget)
    content = "x" * 10_000  # 单条约 10KB，上限只够放一条
    saved_paths: list[Path] = []
    for index in range(3):
        saved = store.put(
            PageRecord(url=f"https://example.com/bytes/{index}", content=content, fetched_at=time.time())
        )
        assert saved is not None
        saved_paths.append(saved)

    files = list((tmp_path / "pages").glob("*.json"))
    stats = store.stats()
    assert len(files) == 1
    assert saved_paths[-1].exists()
    assert stats["entries"] == 1
    assert stats["total_bytes"] == saved_paths[-1].stat().st_size
    assert stats["total_bytes"] <= budget


def test_just_written_record_survives_tiny_byte_budget(tmp_path):
    """字节上限极小（0）时，刚写入的那条仍必须保留。"""

    store = PageStore(tmp_path / "pages", max_total_bytes=0)
    saved = store.put(PageRecord(url="https://example.com/only", content="只有一条", fetched_at=time.time()))
    assert saved is not None
    assert saved.exists()

    stats = store.stats()
    assert stats["entries"] == 1
    assert stats["total_bytes"] > 0
    assert store.get("https://example.com/only") is not None


def test_update_settings_clamps_values(tmp_path):
    """update_settings 把非法参数夹到合法区间，ttl=0 表示不按时间过期。"""

    store = PageStore(tmp_path / "pages", ttl_seconds=120.0, max_entries=8)
    store.update_settings(ttl_seconds=-1.0, max_entries=0, max_total_bytes=-16)

    assert store._ttl_seconds == 0.0
    stats = store.stats()
    assert stats["max_entries"] == 1
    assert stats["max_total_bytes"] == 0

    old = PageRecord(url="https://example.com/never-expire", content="很旧的正文", fetched_at=0.0)
    assert store.put(old) is not None
    loaded = store.get(old.url)
    assert loaded is not None
    assert loaded.content == "很旧的正文"


def test_stats_match_files_on_disk(tmp_path):
    """stats 的 entries/total_bytes 与实际文件一致。"""

    store = PageStore(tmp_path / "pages", max_entries=10)
    for index in range(3):
        saved = store.put(_record(f"https://example.com/stats/{index}", "正文" * index))
        assert saved is not None

    files = sorted((tmp_path / "pages").glob("*.json"))
    stats = store.stats()
    assert stats["entries"] == len(files) == 3
    assert stats["total_bytes"] == sum(path.stat().st_size for path in files)
    assert stats["max_entries"] == 10
    assert stats["max_total_bytes"] == 8 * 1024 * 1024


def test_page_record_dict_roundtrip_and_tolerance():
    """to_dict/from_dict 往返一致；缺失或非法字段按宽容规则兜底。"""

    record = PageRecord(url="https://example.com/a", title="标题", content="正文\n第二行", fetched_at=123.5)
    payload = record.to_dict()
    assert payload == {
        "url": "https://example.com/a",
        "title": "标题",
        "content": "正文\n第二行",
        "fetched_at": 123.5,
    }
    assert PageRecord.from_dict(payload) == record

    empty = PageRecord.from_dict({})
    assert (empty.url, empty.title, empty.content, empty.fetched_at) == ("", "", "", 0.0)

    tolerant = PageRecord.from_dict({"url": 42, "content": 123, "fetched_at": "不是数字"})
    assert tolerant.url == "42"
    assert tolerant.title == ""
    assert tolerant.content == "123"
    assert tolerant.fetched_at == 0.0

    assert PageRecord(url="https://example.com/b", content="字节").byte_size == len("字节".encode("utf-8"))


def test_delete_returns_whether_file_was_removed(tmp_path):
    """delete 只在真的删除文件时返回 True，重复删除返回 False。"""

    store = PageStore(tmp_path / "pages")
    url = "https://example.com/delete"
    saved = store.put(PageRecord(url=url, content="正文", fetched_at=time.time()))
    assert saved is not None

    assert store.delete(url) is True
    assert not saved.exists()
    assert store.delete(url) is False


def test_purge_expired_removes_expired_and_corrupt(tmp_path):
    """purge_expired 删除过期与损坏文件，保留新鲜记录。"""

    store = PageStore(tmp_path / "pages", ttl_seconds=10.0)
    now = time.time()
    fresh = store.put(PageRecord(url="https://example.com/fresh", content="新", fetched_at=now))
    expired = store.put(PageRecord(url="https://example.com/expired", content="旧", fetched_at=now - 100))
    corrupt = store.put(PageRecord(url="https://example.com/corrupt", content="坏", fetched_at=now))
    assert fresh is not None and expired is not None and corrupt is not None
    corrupt.write_text("{broken", encoding="utf-8")

    assert store.purge_expired() == 2
    assert fresh.exists()
    assert not expired.exists()
    assert not corrupt.exists()
    assert store.purge_expired() == 0


def test_put_failure_is_swallowed(tmp_path, caplog):
    """目录不可写时 put 返回 None 并记 warning，不向调用方抛异常。"""

    blocker = tmp_path / "not_a_directory"
    blocker.write_text("占位", encoding="utf-8")
    store = PageStore(blocker, logger=logging.getLogger("tests.pages.write-fail"))
    caplog.set_level(logging.WARNING)

    assert store.put(PageRecord(url="https://example.com/fail", content="正文", fetched_at=time.time())) is None
    assert "pages.save_failed" in caplog.text


def test_public_log_events_are_emitted(tmp_path, caplog):
    """save / hit / evict 三类成功路径日志事件名符合约定。"""

    caplog.set_level(logging.DEBUG)
    store = PageStore(
        tmp_path / "pages",
        ttl_seconds=60.0,
        max_entries=1,
        logger=logging.getLogger("tests.pages.events"),
    )
    url = "https://example.com/log"
    assert store.put(PageRecord(url=url, content="正文", fetched_at=time.time())) is not None
    assert store.get(url) is not None
    assert store.put(PageRecord(url="https://example.com/log-2", content="正文", fetched_at=time.time())) is not None

    assert "pages.save url=" in caplog.text
    assert "pages.hit url=" in caplog.text
    assert "pages.evict" in caplog.text

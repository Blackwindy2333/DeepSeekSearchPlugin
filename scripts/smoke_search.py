"""本地冒烟脚本：对真实 DeepSeek API 走一遍搜索 + 抓取。

用途：部署前确认 Key、网络、代理与返回结构；**会消耗真实额度**（每 1000 次成功请求 5 元）。

用法::

    $env:MAIBOT_DSP_API_KEY = "sk-..."
    .\.venv\Scripts\python.exe scripts/smoke_search.py "DeepSeek 搜索 API"

未传查询时使用默认关键词；``--no-fetch`` 可只测搜索。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_search.client import DeepSeekClient  # noqa: E402
from deepseek_search.settings import mask_secret  # noqa: E402

DEFAULT_QUERY = "DeepSeek 联网搜索 API 使用指南"


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""

    parser = argparse.ArgumentParser(description="DeepSeek 搜索/抓取冒烟脚本")
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY, help="搜索关键词")
    parser.add_argument("--count", type=int, default=3, help="返回条数（1–50）")
    parser.add_argument("--api-key", default="", help="API Key；缺省读 MAIBOT_DSP_API_KEY")
    parser.add_argument("--base-url", default="", help="覆盖 base_url，缺省读 MAIBOT_DSP_BASE_URL")
    parser.add_argument("--proxy", default="", help="代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument("--no-fetch", action="store_true", help="只测搜索，不抓取")
    return parser


async def run(args: argparse.Namespace) -> int:
    """执行一次搜索（并按需抓取首条结果）。"""

    api_key = args.api_key or os.getenv("MAIBOT_DSP_API_KEY", "")
    if not api_key:
        print("[FAIL] 未提供 API Key：请设置 MAIBOT_DSP_API_KEY 或传 --api-key")
        return 2
    base_url = args.base_url or os.getenv("MAIBOT_DSP_BASE_URL", "https://api.deepseek.com")

    print(f"[INFO] base_url={base_url} key={mask_secret(api_key)} query={args.query!r}")
    client = DeepSeekClient(
        api_key,
        base_url=base_url,
        proxy=args.proxy or os.getenv("MAIBOT_DSP_PROXY", ""),
        log_requests=True,
    )
    try:
        print("[INFO] 调用 /v1/tools/search ...")
        result = await client.search(args.query, count=max(1, min(50, args.count)))
        print(f"[OK] 搜索成功：{len(result.items)} 条结果")
        for index, item in enumerate(result.items, start=1):
            print(f"  {index}. {item.title}")
            print(f"     {item.url}")
            print(f"     站点={item.site_name or '-'} 日期={item.date or '-'} 权威度={item.authority} 重排分={item.rerank_score}")
            if item.summary:
                print(f"     摘要：{item.summary[:120]}")

        if args.no_fetch or not result.items:
            return 0
        print("[INFO] 调用 /v1/tools/fetch ...")
        page = await client.fetch(result.items[0].url)
        print(f"[OK] 抓取成功：title={page.title!r} chars={len(page.content)}")
        print(f"     正文开头：{page.content[:200].replace(chr(10), ' ')}")
        return 0
    finally:
        await client.aclose()


def main() -> int:
    """入口：解析参数并运行。"""

    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("[WARN] 已中断")
        return 130
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本只需可读的失败信息
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

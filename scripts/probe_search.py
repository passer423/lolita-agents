"""搜索探针：实测哪种 query 构造能真的把**微博页面**搜出来。

## 为什么需要实测

`weibo` 通道不是直连微博，而是让搜索引擎带出微博页面。这样一来命中率完全取决于
query 怎么写：

* `{q} 微博`            —— "微博"只是一个关键词，搜索引擎可以完全忽略它；
* `site:weibo.com {q}`  —— 站内限定，理论上只返回微博页面；
* `{q} 微博 实拍`       —— 叠加社交语义词，诱导搜索引擎偏向 UGC。

不同 provider（博查 / 智谱）对 `site:` 的支持程度不一样：文档说支持，实际可能
当成普通词处理。这件事**只能跑一遍看数据**——凭文档下结论，和"微博 → 空"是
同一种错误：用预期冒充事实。

## 用法

    python scripts/probe_search.py
    python scripts/probe_search.py --query "Baby 秋日私语" --count 5
    python scripts/probe_search.py --provider zhipu

## 怎么判读

看每个模板的「微博」列：

* 某个模板明显高 —— 换掉 `channels._recall_weibo` 里的 query 构造；
* 三种全是 0     —— 该 provider 根本带不出微博。这时候微博通道应该走
  `m.weibo.cn` 直连，而不是继续挂在搜索引擎下面假装有。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services import web_search  # noqa: E402

# 这里**故意**不复用 `agent.channels._is_weibo`：探针要是和被测代码共用同一份判断，
# 测的就是"我的函数自洽"，而不是真实情况。判断逻辑本身有 bug 时探针也跟着瞎。
WEIBO_HOSTS = ("weibo.com", "weibo.cn")

TEMPLATES = (
    ("postfix", "{q} 微博"),
    ("site", "site:weibo.com {q}"),
    ("social", "{q} 微博 实拍"),
)

DEFAULT_QUERIES = (
    "Baby 秋日私语 JSK",
    "AP 白鸟笼 预约价",
    "花与珍珠匣 缎带 测评",
)


def is_weibo(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == base or host.endswith("." + base) for base in WEIBO_HOSTS)


@dataclass
class Row:
    query: str
    template: str
    text: str
    total: int = 0
    weibo: int = 0
    hosts: list[str] = field(default_factory=list)
    error: str = ""


def probe(query: str, count: int, provider: str) -> list[Row]:
    rows: list[Row] = []
    for name, template in TEMPLATES:
        text = template.format(q=query)
        row = Row(query=query, template=name, text=text)
        try:
            results = web_search.search(text, settings, max_results=count, provider=provider)
        except web_search.SearchUnavailable as exc:
            row.error = str(exc)
            rows.append(row)
            continue
        row.total = len(results)
        row.weibo = sum(1 for item in results if is_weibo(item.url))
        row.hosts = [(urlparse(item.url).hostname or "?") for item in results]
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="实测微博页面在搜索结果里的命中率")
    parser.add_argument("--query", action="append", default=[], help="要测的查询，可重复")
    parser.add_argument("--count", type=int, default=settings.search_max_results, help="每次取几条")
    parser.add_argument("--provider", default=settings.search_provider, help="bocha / zhipu")
    args = parser.parse_args()

    if not settings.search_enabled:
        print("没有 SEARCH_API_KEY，测不了。先在 .env 里配好：")
        print("  SEARCH_ENABLED=true")
        print("  SEARCH_API_KEY=...")
        print("  SEARCH_PROVIDER=bocha")
        return 2

    queries = args.query or list(DEFAULT_QUERIES)
    print(f"provider={args.provider}  count={args.count}  queries={len(queries)}\n")

    totals: dict[str, list[int]] = {name: [] for name, _ in TEMPLATES}
    for query in queries:
        print(f"=== {query} ===")
        for row in probe(query, args.count, args.provider):
            if row.error:
                print(f"  {row.template:<8} {row.text:<44} → 不可用：{row.error}")
                continue
            totals[row.template].append(row.weibo)
            print(f"  {row.template:<8} {row.text:<44} → 返回 {row.total} / 微博 {row.weibo}")
            if row.hosts:
                print(f"            {', '.join(row.hosts[:5])}")
        print()

    print("=== 汇总：每条查询平均命中微博条数 ===")
    for name, _ in TEMPLATES:
        values = totals[name]
        average = sum(values) / len(values) if values else 0.0
        print(f"  {name:<8} {average:.2f}   （样本 {len(values)}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

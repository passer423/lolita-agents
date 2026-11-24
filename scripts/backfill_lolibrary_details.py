"""把 Lolibrary 条目页的尺寸/配色/价格回填到 `data/lolibrary/details.jsonl`。

`catalog.jsonl` 是导入时的身份档案，保持不动；回填结果写独立 sidecar，
一行一条、按 url 覆盖，agent 查本地档案时直接读它，命中后零网络。

特性：

* **可断点续跑**：每抓一条立即 append + flush，进程被杀也不丢已完成的部分；
* **可重复执行只补缺**：sidecar 里 `ok=true` 的条目直接跳过；
* **失败不阻塞**：失败条目记下原因，继续跑后面的，下次执行会重试；
* **有节流**：默认每条间隔 0.6s，叠加 http_fetcher 自带的请求间隔。

用法：

    # 先抽样看覆盖率
    python scripts/backfill_lolibrary_details.py --limit 40 --only-dress

    # 全量回填
    python scripts/backfill_lolibrary_details.py

    # 只看已有 sidecar 的统计，不联网
    python scripts/backfill_lolibrary_details.py --stats-only
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services import lolibrary_backfill, lolibrary_online  # noqa: E402

CATALOG = Path(settings.data_dir) / "lolibrary" / "catalog.jsonl"
DRESS_HINTS = ("OP", "JSK", "SK", "SKIRT", "DRESS", "SALOPETTE", "PINAFORE", "SET")


def load_catalog() -> list[dict]:
    records: list[dict] = []
    with CATALOG.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_sidecar() -> dict[str, dict]:
    """按 url 去重（后写的覆盖先写的），返回已完成/已尝试的记录。"""
    path = lolibrary_backfill.path_for(settings)
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = str(record.get("url") or "")
            if url:
                done[url] = record
    return done


def is_dress(record: dict) -> bool:
    text = f"{record.get('category', '')} {record.get('title', '')}".upper()
    return any(hint in text for hint in DRESS_HINTS)


def has_field(record: dict, names: tuple[str, ...]) -> bool:
    for label, _value in record.get("measurements") or ():
        lowered = str(label).strip().lower()
        if any(name in lowered for name in names):
            return True
    return False


def summarize(records: dict[str, dict], catalog: list[dict]) -> None:
    ok = [record for record in records.values() if record.get("ok")]
    failed = [record for record in records.values() if not record.get("ok")]
    dress_urls = {record["url"] for record in catalog if is_dress(record)}
    dress_ok = [record for record in ok if record.get("url") in dress_urls]

    def coverage(rows: list[dict]) -> dict[str, int]:
        total = len(rows)
        return {
            "total": total,
            "measurements": sum(1 for row in rows if row.get("measurements")),
            "bust": sum(1 for row in rows if has_field(row, ("bust",))),
            "waist": sum(1 for row in rows if has_field(row, ("waist",))),
            "length": sum(1 for row in rows if has_field(row, ("length", "着丈", "总长"))),
            "colors": sum(1 for row in rows if row.get("colors")),
            "price": sum(1 for row in rows if row.get("original_price")),
            "release_year": sum(1 for row in rows if row.get("release_year")),
        }

    def show(label: str, rows: list[dict]) -> None:
        stats = coverage(rows)
        total = stats["total"] or 1
        print(f"\n[{label}] 共 {stats['total']} 条")
        for key in ("measurements", "bust", "waist", "length", "colors", "price", "release_year"):
            print(f"  {key:14s} {stats[key]:4d} 条  {stats[key] / total:6.1%}")

    print(f"sidecar 已尝试 {len(records)} 条：成功 {len(ok)}，失败 {len(failed)}")
    latencies = [record["ms"] for record in ok if isinstance(record.get("ms"), (int, float))]
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"单页耗时：均值 {statistics.mean(latencies):.0f}ms，"
            f"中位 {statistics.median(latencies):.0f}ms，P95 {p95:.0f}ms"
        )
    show("全部条目", ok)
    show("裙子类（OP/JSK/SK/Set）", dress_ok)
    if failed:
        reasons = Counter(
            (record.get("error") or "unknown").splitlines()[0][:80] for record in failed
        )
        print("\n失败原因 Top 5：")
        for reason, count in reasons.most_common(5):
            print(f"  {count:3d}  {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 Lolibrary 条目页尺寸/配色/价格")
    parser.add_argument("--limit", type=int, default=0, help="本轮最多抓多少条（0 = 不限）")
    parser.add_argument("--sleep", type=float, default=0.6, help="每条之间的额外间隔秒数")
    parser.add_argument("--retries", type=int, default=1, help="单条失败后的重试次数")
    parser.add_argument("--only-dress", action="store_true", help="只抓 OP/JSK/SK/Set")
    parser.add_argument("--stats-only", action="store_true", help="只统计已有 sidecar")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    done = load_sidecar()
    if args.stats_only:
        summarize(done, catalog)
        return 0

    todo = [
        record
        for record in catalog
        if record.get("url")
        and not done.get(record["url"], {}).get("ok")
        and (not args.only_dress or is_dress(record))
    ]
    if args.limit > 0:
        todo = todo[: args.limit]
    print(f"目录 {len(catalog)} 条，已成功 {sum(1 for r in done.values() if r.get('ok'))} 条，本轮待抓 {len(todo)} 条")

    sidecar = lolibrary_backfill.path_for(settings)
    sidecar.parent.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    failed = 0
    started_at = time.perf_counter()
    with sidecar.open("a", encoding="utf-8") as handle:
        for index, record in enumerate(todo, start=1):
            url = str(record["url"])
            card = lolibrary_online.SearchCard(
                url=url,
                title=str(record.get("title") or ""),
                brand=str(record.get("brand") or ""),
                category=str(record.get("category") or ""),
                product_number=str(record.get("product_number") or ""),
            )
            detail = None
            error = ""
            began = time.perf_counter()
            for attempt in range(args.retries + 1):
                detail, error = lolibrary_online.fetch_detail(url, card)
                if detail is not None:
                    break
                if attempt < args.retries:
                    time.sleep(args.sleep)
            elapsed_ms = int((time.perf_counter() - began) * 1000)

            entry = {
                "url": url,
                "slug": str(record.get("slug") or ""),
                "ok": detail is not None,
                "error": "" if detail is not None else (error or "empty detail"),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ms": elapsed_ms,
            }
            if detail is not None:
                succeeded += 1
                entry.update(
                    {
                        "title": detail.title,
                        "brand": detail.brand,
                        "category": detail.category,
                        "product_number": detail.product_number,
                        "release_year": detail.release_year,
                        "original_price": detail.original_price,
                        "colors": list(detail.colors),
                        "measurements": [list(pair) for pair in detail.measurements],
                        "alias": detail.alias,
                    }
                )
            else:
                failed += 1
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            done[url] = entry

            if index % 10 == 0 or index == len(todo):
                print(
                    f"  [{index}/{len(todo)}] ok={succeeded} fail={failed} "
                    f"last={elapsed_ms}ms {record.get('title', '')[:40]}"
                )
            time.sleep(args.sleep)

    total_seconds = time.perf_counter() - started_at
    print(f"\n本轮完成：成功 {succeeded}，失败 {failed}，耗时 {total_seconds:.0f}s")
    summarize(done, catalog)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

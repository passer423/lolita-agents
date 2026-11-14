"""把 Lolibrary 的条目抓成**档案缓存**：`data/lolibrary/catalog.jsonl`。

## 它是什么、不是什么

Lolibrary 是档案馆：品牌、分类、发售年份、商品编号、历史原价、尺寸/素材、收录日期。
它**没有当前售价、库存和购买链接**——那些仍然只来自官网/店铺。

所以这份缓存只做"这件是什么"的身份层，供推荐页/agent 匹配；**绝不覆盖**
`content/newarrivals` 里的当前价格和购买链接。

## 用法

    python scripts/import_lolibrary.py --pages 1 --limit 5          # dry-run
    python scripts/import_lolibrary.py --pages 1 --limit 5 --write  # 写缓存

    # 按品牌建档（品牌页分页走的是筛选后的 search，AP 实测有 605 页，只取前两页）
    python scripts/import_lolibrary.py --brand angelic-pretty --pages 2 --no-detail --write

* `--pages`：**每个品牌**最多翻几页（一页 24 条，按入库时间倒序）；
* `--brand`：只抓指定品牌（Lolibrary `/brands/<slug>` 的 slug，可重复）；
  不传就是全站最新入库；
* `--limit`：本轮最多处理几个条目（0 = 不限）；
* `--no-detail`：只抓列表页元数据（标题/品牌/分类/货号），不抓详情页。
  详情页一个条目一次请求，是导入的主要成本；只做品牌/标题匹配时够用；
* `--start-page`：从第几页开始（默认 1）。

抓取复用上游的 `http_fetcher`：只读 GET、固定间隔、429 按 Retry-After 退避。
页面解析也复用上游的纯函数，不在本仓库复制第二份 HTML 结构。

## 为什么按品牌要另拼 URL

`/brands/<slug>` 只显示第一页；它的分页链接实际指向
`/search?brands%5B0%5D=<slug>&sort=added_new&page=N`（实测 AP 有 605 页）。
不按这个模板走，`?page=2` 会被忽略，翻页永远拿到同一页。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import bridge  # noqa: E402
from app.config import settings  # noqa: E402


# 品牌页的第一页是 /brands/<slug>，但它的分页链接指向筛过品牌的 search；
# /brands/<slug>?page=2 会被忽略（实测两页返回同一条）。要翻页只能走这个模板。
BRAND_SEARCH_URL = "https://lolibrary.org/search?brands%5B0%5D={slug}&sort=added_new&page={page}"


def item_image(html: str) -> str:
    """取条目主图；跳过 Lolibrary 的默认占位图。"""
    for tag in re.findall(r"<img[^>]+>", html or "", re.I):
        for attr in ("data-original-url", "src"):
            match = re.search(rf'{attr}="([^"]+)"', tag)
            if not match:
                continue
            url = match.group(1).replace("&amp;", "&")
            if "assets/default.png" in url:
                continue
            return url
    return ""


def record_from_list(entry, *, today: date) -> dict | None:
    """只用列表页元数据拼记录（不抓详情页）。"""
    title = str(entry.title or "").strip()
    if not title:
        return None
    return {
        "slug": entry.slug,
        "url": entry.url,
        "title": title,
        "brand": entry.brand or "",
        "category": entry.category or "",
        "product_number": entry.item_number or "",
        "release_year": "",
        "original_price": "",
        "archived_at": "",
        "country": "",
        "colors": [],
        "features": [],
        "tags": [],
        "measurements": {},
        "notes": "",
        "image": "",
        "fetched_at": today.isoformat(),
    }


def record_from(entry, detail: dict, *, html: str, today: date) -> dict | None:
    record = record_from_list(entry, today=today)
    if record is None:
        return None
    record.update(
        title=str(detail.get("title") or record["title"]).strip(),
        brand=detail.get("brand") or record["brand"],
        category=detail.get("category") or record["category"],
        product_number=detail.get("product_number") or record["product_number"],
        release_year=detail.get("release_year") or "",
        original_price=detail.get("price") or "",
        archived_at=detail.get("published_at") or "",
        country=detail.get("country") or "",
        colors=detail.get("colors") or [],
        features=detail.get("features") or [],
        tags=detail.get("tags") or [],
        measurements=detail.get("measurements") or {},
        notes=detail.get("notes") or "",
        image=item_image(html),
    )
    return record


def merge_records(path: Path, records: list[dict]) -> int:
    existing: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("slug"):
                existing[str(record["slug"])] = record

    for record in records:
        existing[record["slug"]] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in sorted(
            existing.values(),
            key=lambda item: (item.get("archived_at", ""), item.get("slug", "")),
            reverse=True,
        ):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temp.replace(path)
    return len(existing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 Lolibrary 档案到 data/lolibrary/catalog.jsonl")
    parser.add_argument("--pages", type=int, default=1, help="每个品牌最多翻几页（一页 24 条）")
    parser.add_argument("--start-page", type=int, default=1, help="从第几页开始")
    parser.add_argument("--limit", type=int, default=10, help="本轮最多处理几个条目（0 = 不限）")
    parser.add_argument(
        "--brand", action="append", default=[], metavar="SLUG",
        help="只抓这个品牌（/brands/<slug> 的 slug，可重复；不传 = 全站最新入库）",
    )
    parser.add_argument("--no-detail", action="store_true", help="只抓列表页元数据，不抓详情页")
    parser.add_argument("--write", action="store_true", help="写进档案缓存")
    args = parser.parse_args(argv)

    try:
        upstream = bridge.upstream_settings(settings)
        from src.crawler.brand_sites import BrandFetchError, http_fetcher
        from src.crawler.lolibrary import ITEM_URL, SEARCH_URL, parse_lolibrary_item, parse_lolibrary_list
    except Exception as exc:  # noqa: BLE001
        print(f"上游不可用：{type(exc).__name__}: {exc}")
        return 1

    fetch = http_fetcher(upstream)
    today = date.today()
    records: list[dict] = []
    seen: set[str] = set()
    pages = max(1, args.pages)
    start = max(1, args.start_page)

    for brand in args.brand or [""]:
        label = brand or "全站"
        for page in range(start, start + pages):
            if args.limit > 0 and len(records) >= args.limit:
                break
            url = (
                BRAND_SEARCH_URL.format(slug=brand, page=page)
                if brand
                else SEARCH_URL.format(page=page)
            )
            try:
                html = fetch(url)
            except BrandFetchError as exc:
                print(f"[{label}] 列表页失败：{str(exc).splitlines()[0]}")
                break
            entries = parse_lolibrary_list(html)
            print(f"[{label}] 列表第 {page} 页：{len(entries)} 条")
            if not entries:
                break

            for entry in entries:
                if args.limit > 0 and len(records) >= args.limit:
                    break
                if entry.slug in seen:
                    continue
                seen.add(entry.slug)

                if args.no_detail:
                    record = record_from_list(entry, today=today)
                else:
                    try:
                        item_html = fetch(ITEM_URL.format(slug=entry.slug))
                    except BrandFetchError as exc:
                        print(f"  ✗ {entry.slug}：{str(exc).splitlines()[0]}")
                        continue
                    detail = parse_lolibrary_item(item_html)
                    if detail is None:
                        print(f"  ✗ {entry.slug}：不是条目页")
                        continue
                    record = record_from(entry, detail, html=item_html, today=today)
                if record is None:
                    continue
                records.append(record)
                print(
                    f"  → {record['slug']} | {record['brand']} | {record['category']} | "
                    f"{record['product_number'] or '无编号'} | {record['release_year'] or '年份未知'} | "
                    f"{record['original_price'] or '无原价'}"
                )

    print(f"\n本轮解析 {len(records)} 条。")
    if not args.write:
        print("这是 dry-run：没有写档案缓存。确认后加 --write。")
        return 0

    total = merge_records(settings.lolibrary_catalog_path, records)
    print(f"档案缓存：{settings.lolibrary_catalog_path}（共 {total} 条）")
    print("推荐页下次请求会自动读到新档案（按 mtime 缓存失效）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

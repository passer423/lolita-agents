"""按系列名在线查 Lolibrary 的公开档案：只读、不落盘。

本地 `catalog.jsonl` 是 `--no-detail` 的列表导入，只有标题/品牌/品类/货号；
用户要的发售年份、历史原价、配色必须到条目页取。这里只做最小解析：

* 搜索页：按卡片切分，取条目链接、标题、品牌、品类、货号；
* 条目页：去脚本/标签后按文本正则取 `Released in` / `Originally listed for` /
  `Product number` / `Official colors`。

任何网络或解析失败都返回空 + 可读错误，由上层标「未查到」，**不抛异常、不猜**。
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from app.services import imagefetch

logger = logging.getLogger(__name__)

BASE = "https://lolibrary.org"
SEARCH_URL = BASE + "/search?search={query}"
MAX_CARDS = 24

_CARD_SPLIT = re.compile(r'<div class="card">', re.I)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_BRAND_LINK = re.compile(r'href="[^"]*/brands/[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
_CATEGORY_LINK = re.compile(r'href="[^"]*/categories/([^"/]+)"[^>]*>(.*?)</a>', re.I | re.S)
_PRODUCT_NUM = re.compile(r'class="[^"]*itemnum[^"]*"[^>]*title="([^"]+)"', re.I)
_ALIAS = re.compile(r'<p[^>]*class="[^"]*text-muted small[^"]*"[^>]*title="([^"]+)"', re.I | re.S)
_TAG = re.compile(r"<[^>]{1,200}>")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"\s+")

_RELEASE = re.compile(r"Released in\s+((?:19|20)\d{2})", re.I)
_PRODUCT_NUMBER = re.compile(r"Product number:\s*([A-Za-z0-9-]{3,})", re.I)
_PRICE = re.compile(r"Originally listed for\s*([¥￥]\s*[\d,]+(?:\s*[A-Z]{3})?)", re.I)
_COLORS_START = re.compile(r"Official colors|Colorways", re.I)
_COLORS_STOP = re.compile(r"Features|Measurements|Product number|Tags|Notes|Images", re.I)

# 颜色词表：只认封闭词，避免把面料/工艺词当颜色。长词优先（sax blue 先于 blue）。
_COLOR_WORDS = (
    "offwhite", "off-white", "sax blue", "sax", "pink", "white", "black", "ivory",
    "cream", "red", "wine", "burgundy", "purple", "lavender", "green", "mint",
    "gray", "grey", "brown", "gold", "silver", "blue", "navy", "yellow", "beige",
    "orange", "champagne",
)
_COLOR_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(word) for word in _COLOR_WORDS) + r")(?![a-z])",
    re.I,
)

_DRESS_CATEGORIES = {
    "op", "jsk", "sk", "skirt", "dress", "one piece", "salopette", "pinafore",
    "jumper skirt",
}
_DRESS_TITLE_RE = re.compile(r"\b(OP|JSK|SK|SKIRT|DRESS)\b", re.I)
_STOP_WORDS = {"the", "and", "for", "with", "pour", "les", "des", "build", "order"}


@dataclass(frozen=True, slots=True)
class SearchCard:
    url: str
    title: str
    brand: str = ""
    category: str = ""
    product_number: str = ""
    alias: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveDetail:
    url: str
    title: str
    brand: str = ""
    category: str = ""
    product_number: str = ""
    release_year: str = ""
    original_price: str = ""
    colors: tuple[str, ...] = ()
    alias: str = ""


def _text(raw: str) -> str:
    text = _TAG.sub(" ", raw or "")
    return _WS.sub(" ", html_lib.unescape(text)).strip()


def _plain(html: str) -> str:
    stripped = _SCRIPT_STYLE.sub("\n", html or "")
    text = _TAG.sub("\n", stripped)
    return _WS.sub(" ", html_lib.unescape(text))


def _item_url(href: str) -> str:
    return urljoin(BASE + "/", (href or "").strip())


def parse_cards(html: str) -> list[SearchCard]:
    """解析搜索页卡片；同一 URL 只留第一张。"""
    parts = _CARD_SPLIT.split(html or "")
    cards = parts[1:] if len(parts) > 1 else [html or ""]
    found: list[SearchCard] = []
    seen: set[str] = set()
    for card in cards[:MAX_CARDS]:
        title = ""
        url = ""
        for match in _ANCHOR_RE.finditer(card):
            href = match.group(1)
            text = _text(match.group(2))
            if "/items/" not in href or not text or text.lower() == "view item":
                continue
            url = _item_url(href)
            title = text
            break
        if not url or url in seen:
            continue
        seen.add(url)
        brand_match = _BRAND_LINK.search(card)
        category_match = _CATEGORY_LINK.search(card)
        number_match = _PRODUCT_NUM.search(card)
        alias_match = _ALIAS.search(card)
        found.append(
            SearchCard(
                url=url,
                title=title,
                brand=_text(brand_match.group(1)) if brand_match else "",
                category=_text(category_match.group(2)) if category_match else "",
                product_number=_text(number_match.group(1)) if number_match else "",
                alias=_text(alias_match.group(1)) if alias_match else "",
            )
        )
    return found


def _extract_colors(text: str) -> tuple[str, ...]:
    match = _COLORS_START.search(text or "")
    if not match:
        return ()
    window = text[match.end(): match.end() + 1200]
    stop = _COLORS_STOP.search(window, 1)
    if stop:
        window = window[: stop.start()]
    colors: list[str] = []
    for word in _COLOR_RE.findall(window):
        normalized = word.lower().replace("-", "")
        if normalized not in {item.lower().replace("-", "") for item in colors}:
            colors.append(word.title() if word.isascii() else word)
    return tuple(colors[:8])


def parse_item(html: str, card: SearchCard) -> ArchiveDetail:
    """解析条目页字段；拿不到就留空，不猜。"""
    text = _plain(html)
    release = _RELEASE.search(text)
    number = _PRODUCT_NUMBER.search(text)
    price = _PRICE.search(text)
    colors = _extract_colors(text)
    return ArchiveDetail(
        url=card.url,
        title=card.title,
        brand=card.brand,
        category=card.category,
        product_number=_text(number.group(1)) if number else card.product_number,
        release_year=release.group(1) if release else "",
        original_price=_text(price.group(1)) if price else "",
        colors=colors,
        alias=card.alias,
    )


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
        if token not in _STOP_WORDS
    }


def _is_dress(card: SearchCard) -> bool:
    category = (card.category or "").strip().lower()
    return category in _DRESS_CATEGORIES or bool(_DRESS_TITLE_RE.search(card.title or ""))


def search(query: str, *, fetcher=None) -> tuple[list[SearchCard], str]:
    """搜索 Lolibrary；返回 ``(卡片, 错误)``，失败不抛。"""
    query = (query or "").strip()
    if not query:
        return [], ""
    fetch = fetcher or imagefetch.fetch_html
    try:
        html = fetch(SEARCH_URL.format(query=quote(query)))
    except Exception as exc:  # noqa: BLE001 - 搜索失败由上层降级
        logger.info("Lolibrary 在线搜索失败：%s: %s", type(exc).__name__, exc)
        return [], f"Lolibrary 搜索失败：{type(exc).__name__}"
    return parse_cards(html), ""


def fetch_detail(url: str, card: SearchCard, *, fetcher=None) -> tuple[ArchiveDetail | None, str]:
    """抓一个条目详情页；失败返回 ``(None, 错误)``。"""
    fetch = fetcher or imagefetch.fetch_html
    try:
        html = fetch(url)
    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整次查询
        logger.info("Lolibrary 条目抓取失败 %s：%s", url, exc)
        return None, f"条目抓取失败：{type(exc).__name__}"
    return parse_item(html, card), ""


def lookup(
    query: str,
    *,
    brand: str = "",
    fetcher=None,
    max_items: int = 2,
) -> tuple[list[ArchiveDetail], str]:
    """按系列名查在线档案：只返回裙子类目，最多抓 ``max_items`` 条详情。"""
    cards, error = search(query, fetcher=fetcher)
    if error:
        return [], error

    wanted_brand = (brand or "").strip().lower()
    candidates = [
        card for card in cards
        if _is_dress(card)
        and (not wanted_brand or wanted_brand in (card.brand or "").lower())
    ]
    if not candidates:
        return [], "Lolibrary 没有搜到裙子条目"

    query_tokens = _tokens(query)
    order = {card.url: index for index, card in enumerate(candidates)}
    candidates.sort(
        key=lambda card: (
            -len(query_tokens & _tokens(card.title)),
            order[card.url],
        )
    )

    details: list[ArchiveDetail] = []
    errors: list[str] = []
    for card in candidates[: max(1, max_items)]:
        detail, detail_error = fetch_detail(card.url, card, fetcher=fetcher)
        if detail is not None:
            details.append(detail)
        elif detail_error:
            errors.append(detail_error)
    if details:
        return details, ""
    return [], errors[0] if errors else "Lolibrary 条目页没有可用字段"

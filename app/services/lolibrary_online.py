"""按系列名在线查 Lolibrary 的公开档案：只读、不落盘。

本地 `catalog.jsonl` 是 `--no-detail` 的列表导入，只有标题/品牌/品类/货号；
用户要的发售年份、发售价、配色必须到条目页取。这里只做最小解析：

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
# 币种写法不统一：站内是 `CN¥598`，日牌是 `¥12000 JPY`，也可能写成 `598 CNY` / `RMB598`。
_CURRENCY = r"(?:CN¥|CN\$|CNY|RMB|JPY|USD|EUR|GBP|¥|￥|\$)"
_PRICE = re.compile(
    r"Originally listed for\s*("
    rf"{_CURRENCY}\s*[\d,]+(?:\.\d+)?(?:\s*(?:CNY|RMB|JPY|USD|EUR|GBP))?"
    r"|[\d,]+(?:\.\d+)?\s*(?:CNY|RMB|JPY|USD|EUR|GBP)"
    r")",
    re.I,
)
_COLORS_START = re.compile(r"Official colors|Colorways", re.I)
_COLORS_STOP = re.compile(r"Features|Measurements|Product number|Tags|Notes|Images", re.I)

# 条目页的平铺尺寸是 `<h4>Bust</h4><p>88 - 94 cm …</p>` 这种成对结构。
# 只认带 cm/in 数值的块，Material / Notes 这类文字段落不会命中。
_MEASUREMENT_BLOCK = re.compile(
    r"<h4\b[^>]*>\s*([^<]{1,40}?)\s*</h4>\s*<p\b[^>]*>(.*?)</p>",
    re.I | re.S,
)
_MEASUREMENT_VALUE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:cm|in)\b",
    re.I,
)

# `[Price note]` 是自由文本，逐档价只认「金额 + (short|long length)」成对出现的写法。
_PRICE_NOTE = re.compile(r"\[\s*Price note\s*\](.*?)(?:\[|\bRerelease\b|$)", re.I | re.S)
_TIER = re.compile(
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<currency>CN¥|CNY|RMB|JPY|USD|EUR|GBP|¥|￥)?\s*"
    r"\(\s*(?P<length>short|long)(?:\s+length)?\s*\)",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+")
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)")

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
class PriceTier:
    """一档价格：`stage`（normal / preorder / ready_stock）× `length`（short / long）。"""

    stage: str
    length: str
    amount: str
    currency: str = ""


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
    #: 平铺尺寸，按条目页出现顺序：`(("Bust", "88 - 94 cm …"), ("Waist", "70 - 75 cm …"))`。
    measurements: tuple[tuple[str, str], ...] = ()
    alias: str = ""
    price_tiers: tuple[PriceTier, ...] = ()


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


def _extract_measurements(html: str) -> tuple[tuple[str, str], ...]:
    """取条目页的平铺尺寸（Bust / Waist / Length…）。

    只收「标题 + 带 cm/in 数值」的成对块：Material、Notes 这类没有尺寸数字，
    不会被当成尺寸；同一标题重复出现只留第一次。
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _MEASUREMENT_BLOCK.finditer(html or ""):
        label = _text(match.group(1)).rstrip(":").strip()
        value = _text(match.group(2))
        key = label.lower()
        if not label or not value or key in seen:
            continue
        if not _MEASUREMENT_VALUE.search(value):
            continue
        seen.add(key)
        found.append((label, value))
    return tuple(found[:8])


def _amount(value: str) -> tuple[str, str]:
    """把 `CN¥598` / `558 CNY` / `¥12000 JPY` 拆成 ``(数字, 币种)``。"""
    match = _AMOUNT.search(value or "")
    if not match:
        return "", ""
    prefix = value[: match.start()]
    currency = ""
    for token in ("CN¥", "CN$", "CNY", "RMB", "JPY", "USD", "EUR", "GBP", "¥", "￥", "$"):
        if token in prefix:
            currency = token
            break
    if not currency:
        suffix = value[match.end():]
        for token in ("CNY", "RMB", "JPY", "USD", "EUR", "GBP"):
            if token.lower() in suffix.lower():
                currency = token
                break
    if currency in ("CN¥", "CNY", "RMB"):
        currency = "CNY"
    return match.group(1).replace(",", ""), currency


def _tier_stage(sentence: str) -> str:
    lowered = (sentence or "").lower()
    if "preorder" in lowered or "pre-order" in lowered:
        return "preorder"
    if "ready stock" in lowered or "in stock" in lowered:
        return "ready_stock"
    return "normal"


def _parse_price_tiers(text: str) -> tuple[PriceTier, ...]:
    """解析 `[Price note]` 里的逐档价（预约价/现货价 × 短款/长款）。

    只认「金额 + (short length) / (long length)」成对出现的写法；某个配色单独加价这类
    没有长短款成对标注的句子直接跳过——宁可留空，也不把数字硬填到某一档上。
    """
    note = _PRICE_NOTE.search(text or "")
    if not note:
        return ()
    tiers: list[PriceTier] = []
    for sentence in _SENTENCE_SPLIT.split(note.group(1)):
        stage = _tier_stage(sentence)
        for match in _TIER.finditer(sentence):
            amount, currency = _amount(match.group(0))
            if not amount:
                continue
            tiers.append(
                PriceTier(
                    stage=stage,
                    length=match.group("length").lower(),
                    amount=amount,
                    currency=currency,
                )
            )
    return tuple(tiers)


def parse_item(html: str, card: SearchCard) -> ArchiveDetail:
    """解析条目页字段；拿不到就留空，不猜。"""
    text = _plain(html)
    release = _RELEASE.search(text)
    number = _PRODUCT_NUMBER.search(text)
    price = _PRICE.search(text)
    colors = _extract_colors(text)
    original_price = _text(price.group(1)) if price else ""
    tiers = _parse_price_tiers(text)
    amount, currency = _amount(original_price)
    if amount and not any(tier.stage == "normal" for tier in tiers):
        tiers = (PriceTier(stage="normal", length="", amount=amount, currency=currency), *tiers)
    return ArchiveDetail(
        url=card.url,
        title=card.title,
        brand=card.brand,
        category=card.category,
        product_number=_text(number.group(1)) if number else card.product_number,
        release_year=release.group(1) if release else "",
        original_price=original_price,
        colors=colors,
        measurements=_extract_measurements(html),
        alias=card.alias,
        price_tiers=tiers,
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
    """按系列名查在线档案：优先裙子类目，没有裙子时也接受小物。

    发饰/包/袜/鞋这类条目同样有尺码信息，不能因为类目被整条丢掉；
    排序时裙子只作为同分词数下的优先项，不硬性排除小物。
    """
    cards, error = search(query, fetcher=fetcher)
    if error:
        return [], error

    wanted_brand = (brand or "").strip().lower()
    candidates = [
        card for card in cards
        if not wanted_brand or wanted_brand in (card.brand or "").lower()
    ]
    if not candidates:
        return [], "Lolibrary 没有搜到条目"

    query_tokens = _tokens(query)
    order = {card.url: index for index, card in enumerate(candidates)}
    candidates.sort(
        key=lambda card: (
            -len(query_tokens & _tokens(card.title)),
            0 if _is_dress(card) else 1,
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

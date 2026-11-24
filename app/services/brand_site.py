"""品牌官网情报源（试点：Angelic Pretty / BABY, THE STARS SHINE BRIGHT）。

只做一件事：把官网「新作 / 入荷」列表页解析成 ``标题 + 日期 + 链接``，作为
``news`` 意图的第一来源。抓不到就返回空并让调用方降级到微博 / 网页，绝不
凭标题编造条目。

设计约束：

* **缓存**：每个品牌一个 JSON 文件，默认 30 分钟。官网列表短时间内不会变，
  没必要每次提问都去抓；空结果也写入缓存，避免页面结构变化后反复打官网。
* **超时**：单站总预算 8 秒（含多页串行），到点立即放弃、不再干等；超时
  优先退回 6 小时内的旧缓存并标 ``stale``，没有旧缓存才报「官网暂时没响应」。
  缓存命中直接返回，不进入网络路径。
* **容忍小改**：优先按标签 + class 识别列表块；class 改名时退回按
  ``/Page/Feature/``、``<time>``、``<h2>`` 的顺序配对。解析不出条目就是空，
  不猜。
* **编码**：两个站点都是 UTF-8；仍按响应头 charset → UTF-8 → CP932 依次尝试，
  避免个别页面回退成乱码。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.config import AppSettings

logger = logging.getLogger(__name__)

# 单页 socket 超时。AP 列表本机实测约 1.5s，BABY 约 0.7s；6s 足够覆盖慢网。
REQUEST_TIMEOUT = 6.0
# 单站（含该站多个页面的串行抓取）总预算。硬上限：到点就放弃，不再等下一页。
SITE_TIMEOUT = 8.0
CACHE_TTL = 30 * 60
STALE_TTL = 6 * 60 * 60
MAX_ITEMS_PER_SITE = 4
MAX_ITEMS_TOTAL = 6

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    # 两个站点都是日文站；不带日语优先会拿到英文/机器翻译版本。
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
}

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# 官网列表里的「新作/入荷」标题关键词。日文站和 BABY 的英文品牌公告混用，
# 所以中英日三种都收。命中才收录，避免把门店活动、会员日、系统维护当成新作。
_NEWS_KEYWORDS = (
    "入荷", "発売", "販売", "予約", "新作", "新商品", "新色", "再販",
    "コレクション", "登場",
    "collection", "new arrival", "new item", "release", "pre-order", "preorder",
    "brand-new", "new series",
)
# BABY 的 /news/ 同时混入 gallery（活动回顾）和 media（媒体报道）：它们是
# 「报道/回顾」，不是品牌发布的新作/入荷公告，不能占 news 的首选位置。
_EXCLUDED_URL_MARKERS = ("/gallery/", "/media/")

_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
)
_YEAR_MONTH_PATTERNS = (
    re.compile(r"(20\d{2})[-/.](\d{1,2})(?![-/.\d])"),
    re.compile(r"(20\d{2})年(\d{1,2})月"),
)


class BrandSiteUnavailable(RuntimeError):
    """官网源不可用（没有登记品牌 / 全部页面抓取失败）。"""


@dataclass(frozen=True, slots=True)
class BrandSite:
    id: str
    name: str
    aliases: tuple[str, ...]
    pages: tuple[str, ...]
    # 列表块：优先按标签 + class 片段识别；class 改名时解析器会退回通用配对。
    block_tag: str = ""
    block_markers: tuple[str, ...] = ()

    def matches(self, question: str) -> bool:
        lowered = (question or "").lower()
        return any(_alias_hit(alias, lowered) for alias in self.aliases)


SITES: tuple[BrandSite, ...] = (
    BrandSite(
        id="angelic_pretty",
        name="Angelic Pretty",
        aliases=("angelic pretty", "angelicpretty", "ap", "安琪"),
        pages=(
            # SHOPNEWS：入荷 / 发售 / 预约类公告。
            "https://angelicpretty.com/Form/FeaturePage/FeaturePageList.aspx?fpcid=005",
            # TOPICS：新作 / 活动 / 系列情报。
            "https://angelicpretty.com/Form/FeaturePage/FeaturePageList.aspx?fpcid=003",
        ),
        block_tag="li",
        block_markers=("archive__col",),
    ),
    BrandSite(
        id="baby",
        name="BABY, THE STARS SHINE BRIGHT",
        aliases=(
            "baby, the stars shine bright",
            "baby,the stars shine bright",
            "baby the stars shine bright",
            "btssb",
            "babyssb",
            "baby",
        ),
        pages=("https://www.babyssb.co.jp/news/",),
        # BABY 的列表项就是 <article>；不锁 class，class 改名也能继续解析。
        block_tag="article",
        block_markers=(),
    ),
)


def _alias_hit(alias: str, text: str) -> bool:
    alias = (alias or "").strip().lower()
    text = (text or "").lower()
    if not alias:
        return False
    if alias.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def _normalize_date(value: str) -> str:
    value = (value or "").strip()
    for pattern in _DATE_PATTERNS:
        match = pattern.search(value)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    for pattern in _YEAR_MONTH_PATTERNS:
        match = pattern.search(value)
        if match:
            year, month = match.groups()
            return f"{year}-{int(month):02d}"
    return ""


def _date_from_url(url: str) -> str:
    """AP 的 Feature 链接用 ``YYYYMM`` / ``YYYYMMDD`` 编码日期，列表页无 time 标签。"""
    path = url.split("?", 1)[0]
    match = re.search(r"/(20\d{2})(\d{2})(\d{2})(?:\D|$)", path)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"/(20\d{2})(\d{2})(?:\D|$)", path)
    if match:
        year, month = match.groups()
        return f"{year}-{int(month):02d}"
    return ""


def _is_news_title(title: str) -> bool:
    lowered = (title or "").lower()
    return any(keyword in lowered for keyword in _NEWS_KEYWORDS)


class _ListParser(HTMLParser):
    """从列表页里抽 ``标题 + 日期 + 链接``。

    只跟踪当前块（AP 的 ``li.archive__col`` / BABY 的 ``article``）内的第一个
    链接、第一个 h2/h3、第一个 time。DOM 小改（class 改名、标题从 h3 换 h2、
    日期从属性换文本）都能继续工作；结构大改则解析为空。
    """

    def __init__(self, site: BrandSite) -> None:
        super().__init__(convert_charrefs=True)
        self.site = site
        self.items: list[dict] = []
        self._depth = 0
        self._block: dict | None = None
        self._block_depth = 0
        self._title_parts: list[str] | None = None
        self._title_depth = -1
        self._time_parts: list[str] | None = None
        self._time_depth = -1

    def _start_block(self, attrs: dict[str, str]) -> None:
        classes = attrs.get("class", "")
        if self.site.block_markers and not any(
            marker in classes for marker in self.site.block_markers
        ):
            return
        self._block = {"hrefs": [], "title": "", "date": "", "text": ""}
        self._block_depth = self._depth

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): (value or "") for key, value in attrs}
        if self._block is None and tag == self.site.block_tag:
            self._start_block(values)

        if self._block is not None:
            if tag == "a" and values.get("href"):
                self._block["hrefs"].append(values["href"])
            elif tag in ("h2", "h3") and self._title_parts is None and not self._block["title"]:
                self._title_parts = []
                self._title_depth = self._depth
            elif tag == "time":
                stamp = values.get("datetime", "").strip()
                if stamp and not self._block["date"]:
                    self._block["date"] = stamp
                elif not stamp and self._time_parts is None:
                    self._time_parts = []
                    self._time_depth = self._depth

        if tag not in _VOID_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _VOID_TAGS:
            self._depth = max(0, self._depth - 1)

        if self._block is None:
            return
        if self._title_parts is not None and tag in ("h2", "h3") and self._depth == self._title_depth:
            title = " ".join("".join(self._title_parts).split())
            if title and not self._block["title"]:
                self._block["title"] = title
            self._title_parts = None
        if self._time_parts is not None and tag == "time" and self._depth == self._time_depth:
            stamp = "".join(self._time_parts).strip()
            if stamp and not self._block["date"]:
                self._block["date"] = stamp
            self._time_parts = None
        if tag == self.site.block_tag and self._depth == self._block_depth:
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if self._block is None:
            return
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._time_parts is not None:
            self._time_parts.append(data)
        if len(self._block["text"]) < 600:
            self._block["text"] += data

    def _finish_block(self) -> None:
        assert self._block is not None
        hrefs = self._block["hrefs"]
        # AP 的块里还有一条 READ MORE；优先选 Feature / news 链接。
        href = next(
            (
                item
                for item in hrefs
                if any(marker in item for marker in ("/Page/Feature/", "/official_news/", "/news/"))
            ),
            hrefs[0] if hrefs else "",
        )
        title = " ".join(self._block["title"].split())
        date = (
            _normalize_date(self._block["date"])
            or _normalize_date(self._block["text"])
            or _date_from_url(href)
        )
        if title and href:
            self.items.append({"title": title, "date": date, "url": href})
        self._block = None
        self._title_parts = None
        self._time_parts = None


def _fallback_pairs(html: str) -> list[dict]:
    """主解析器没结果时的兜底：按页面顺序配对 Feature 链接与 h2/h3。"""
    links: list[str] = []
    for match in re.finditer(r"""href=["']([^"']*(?:/Page/Feature/|/official_news/|/news/)[^"']*)["']""", html):
        href = match.group(1)
        if href not in links:
            links.append(href)
    titles = [
        " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())
        for match in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", html, re.S | re.I)
    ]
    titles = [title for title in titles if title]
    return [
        {"title": title, "date": _date_from_url(href), "url": href}
        for href, title in zip(links, titles, strict=False)
    ]


def parse_news_page(html: str, page_url: str, site: BrandSite) -> list[dict]:
    """解析一页；返回绝对链接。解析不出来返回空表。"""
    if not html:
        return []
    parser = _ListParser(site)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - 第三方页面畸形时不能拖垮整条通道
        logger.info("官网页面解析失败 %s：%s", page_url, exc)
        return []
    items = parser.items or _fallback_pairs(html)
    result: list[dict] = []
    seen: set[str] = set()
    for item in items:
        url = urljoin(page_url, item["url"]).split("#", 1)[0]
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        seen.add(url)
        result.append({"title": title, "date": item.get("date") or "", "url": url})
    return result


def _is_relevant(item: dict) -> bool:
    url = str(item.get("url") or "")
    if any(marker in url for marker in _EXCLUDED_URL_MARKERS):
        return False
    return _is_news_title(str(item.get("title") or ""))


def _rank(items: list[dict]) -> list[dict]:
    """只留新作/入荷标题，按日期倒序；没有日期的排后面。"""
    news = [item for item in items if _is_relevant(item)]
    news.sort(key=lambda item: item.get("date") or "0000-00", reverse=True)
    return news


def _fetch(url: str, timeout: float = REQUEST_TIMEOUT) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    # 延迟导入：只有真正抓官网时才需要证书包，测试可以完全不碰网络。
    from app.services import imagefetch

    with urlopen(request, timeout=timeout, context=imagefetch.ssl_context()) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
    for encoding in (charset, "utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode(charset, errors="replace")


def _fetch_limited(url: str, timeout: float) -> str:
    """带硬超时的单页抓取。

    ``urlopen`` 的 timeout 只约束单次 socket 读写，服务端慢速回包时总时长仍
    可能超过它；单站预算是硬上限，所以放到 daemon 线程里 join 兜底，到点直接
    放弃（线程自行结束，不阻塞调用方）。
    """
    result: dict[str, object] = {}

    def worker() -> None:
        try:
            result["html"] = _fetch(url, timeout=timeout)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常交回调用方处理
            result["error"] = exc

    thread = threading.Thread(target=worker, name="brand-site-fetch", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"超过 {timeout:.1f} 秒未返回")
    error = result.get("error")
    if isinstance(error, BaseException):
        raise error
    return str(result.get("html") or "")


def _cache_path(app: AppSettings, site_id: str) -> Path:
    return Path(app.data_dir) / "cache" / "brand_site" / f"{site_id}.json"


def _read_cache(app: AppSettings, site_id: str) -> dict | None:
    path = _cache_path(app, site_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    try:
        fetched_at = float(data.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None
    return {"fetched_at": fetched_at, "items": data["items"]}


def _write_cache(app: AppSettings, site_id: str, items: list[dict]) -> None:
    path = _cache_path(app, site_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        payload = {"fetched_at": time.time(), "items": items}
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        # 缓存写不了不是错误：下次重新抓。
        logger.info("官网缓存写入失败 %s：%s", path, exc)


def _with_brand(items: list[dict], site: BrandSite, *, stale: bool = False) -> list[dict]:
    result = []
    for item in items[:MAX_ITEMS_PER_SITE]:
        entry = {
            "brand": site.name,
            "site_id": site.id,
            "title": str(item.get("title") or ""),
            "date": str(item.get("date") or ""),
            "url": str(item.get("url") or ""),
        }
        if stale:
            entry["stale"] = True
        result.append(entry)
    return result


def _site_items(site: BrandSite, app: AppSettings) -> list[dict]:
    """一个品牌的情报条目：新鲜缓存 → 抓取 → 旧缓存 → 报不可用。"""
    now = time.time()
    cached = _read_cache(app, site.id)
    if cached and now - cached["fetched_at"] <= CACHE_TTL:
        # 缓存命中不走网络，也不受单站预算影响：这条路径应当接近 0ms。
        return _with_brand(cached["items"], site)

    deadline = time.monotonic() + SITE_TIMEOUT
    items: list[dict] = []
    errors: list[str] = []
    fetched = 0
    timed_out = False
    for page in site.pages:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            errors.append(f"{page}：单站 {SITE_TIMEOUT:.0f} 秒预算用完，跳过")
            break
        try:
            html = _fetch_limited(page, min(REQUEST_TIMEOUT, remaining))
        except TimeoutError as exc:
            timed_out = True
            errors.append(f"{page}：{exc}")
            # 到点就放弃整站剩下的页面，不再串行等下一页。
            break
        except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮同品牌其他页
            errors.append(f"{page}：{type(exc).__name__}: {str(exc)[:80]}")
            continue
        fetched += 1
        items.extend(parse_news_page(html, page, site))

    # 超时：整个站点没有在预算内抓完，优先用 6 小时内的旧缓存并标明旧数据；
    # 连旧缓存都没有就如实报「官网暂时没响应」，不拿半页结果冒充完整上新。
    if timed_out:
        if cached and time.time() - cached["fetched_at"] <= STALE_TTL:
            logger.warning(
                "官网超过 %.0f 秒未响应，使用 %.0f 分钟前的缓存：%s",
                SITE_TIMEOUT,
                (time.time() - cached["fetched_at"]) / 60,
                site.id,
            )
            return _with_brand(cached["items"], site, stale=True)
        raise BrandSiteUnavailable(f"官网暂时没响应（单站 {SITE_TIMEOUT:.0f} 秒超时）")

    if fetched:
        seen: set[str] = set()
        unique = []
        for item in items:
            url = str(item.get("url") or "")
            if url and url in seen:
                continue
            seen.add(url)
            unique.append(item)
        ranked = _rank(unique)[:MAX_ITEMS_PER_SITE]
        _write_cache(app, site.id, ranked)
        return _with_brand(ranked, site)

    if cached and time.time() - cached["fetched_at"] <= STALE_TTL:
        logger.warning("官网抓取失败，使用 %.0f 分钟前的缓存：%s", (time.time() - cached["fetched_at"]) / 60, site.id)
        return _with_brand(cached["items"], site, stale=True)

    raise BrandSiteUnavailable("；".join(errors) or "官网没有返回内容")


def match_sites(question: str) -> list[BrandSite]:
    """问题里点名的已登记品牌；多品牌按最长别名排序，不猜。"""
    hits: list[tuple[int, BrandSite]] = []
    for site in SITES:
        lengths = [len(alias) for alias in site.aliases if _alias_hit(alias, question)]
        if lengths:
            hits.append((max(lengths), site))
    hits.sort(key=lambda item: item[0], reverse=True)
    return [site for _length, site in hits]


def lookup(question: str, app: AppSettings) -> list[dict]:
    """按问题里的品牌抓官网新作/入荷条目；没有登记品牌或全部失败时抛出。"""
    sites = match_sites(question)
    if not sites:
        raise BrandSiteUnavailable("问题里没有已登记官网的品牌（试点：Angelic Pretty、BABY）")

    items: list[dict] = []
    errors: list[str] = []
    succeeded = 0
    for site in sites:
        try:
            site_items = _site_items(site, app)
        except BrandSiteUnavailable as exc:
            errors.append(f"{site.name}：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 单站点异常按失败降级
            errors.append(f"{site.name}：{type(exc).__name__}: {str(exc)[:80]}")
            continue
        succeeded += 1
        items.extend(site_items)

    if not items and not succeeded:
        raise BrandSiteUnavailable("官网抓取失败：" + "；".join(errors))
    return items[:MAX_ITEMS_TOTAL]


def readiness(_app: AppSettings | None = None) -> tuple[bool, str]:
    """状态展示用：模块已登记站点即就绪，不代表当前网络一定可达。"""
    names = "、".join(site.name for site in SITES)
    return True, f"试点：{names}（缓存 30 分钟；单站 8 秒；超时用 6 小时内旧缓存，无缓存降级微博/网页）"

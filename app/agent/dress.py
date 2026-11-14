"""裙子档案工具：识别品牌/地区，按日牌/国牌选择来源，整合字段。

这不是一个新的召回通道：不加入 ``channels.ALL_CHANNELS``，只由 graph 的
``dress_resolve`` / ``dress_profile`` 两个节点调用。

职责边界：

* **识别**：商品编号、档案标题/中文别名、系列译名表、品牌别名；都查不到品牌时
  标 ``UNKNOWN``，由 graph 决定「两边都查」。
* **路由**：日牌 → Lolibrary；国牌 → 小红书 + 微博；未识别 → 两边都查。
* **取数**：日牌优先本地档案，字段不全时在线补条目详情；国牌从社媒证据里抽
  价格/发售/款式/配色，全部标「网友提到，未核实」。
* **整合**：输出结构化档案 + 一条 ``channel="dress"`` 证据，交给 writer。

所有网络失败都降级为「未查到」，不抛异常、不猜字段。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import date

from app.agent import channels
from app.agent.channels import RecallContext
from app.config import AppSettings
from app.config import settings as default_settings
from app.services import lolibrary_online

logger = logging.getLogger(__name__)

SOCIAL_CHANNELS = ("xhs", "weibo")
EVIDENCE_TEXT_LIMIT = 300

# 「信息字段意图」：用户问的是裙子的档案字段，而不是洗护/尺码/搭配。
FIELD_MARKERS = (
    "发售", "上市", "出荷", "价格", "多少钱", "多少", "原价", "预约", "定金",
    "现货", "配色", "颜色", "款式", "什么型", "什么时候", "哪年", "哪一年",
    "介绍", "资料", "品牌", "日牌", "国牌", "哪家", "出品",
)

# 洗护/尺码/搭配类问题不触发裙子档案：这类问题该走知识库，不该被拽去查价格。
CARE_MARKERS = (
    "怎么洗", "洗护", "清洗", "保养", "晾", "尺码", "尺寸", "显胖",
    "搭配", "怎么穿", "推荐", "值得买", "适合", "新手",
)

_DRESS_MARKERS = ("裙子", "连衣裙", "小裙子", "半裙", "jsk", "op", "sk", "套装", "这条", "这款")
_CATEGORY_HINTS = (
    ("jsk", "JSK"), ("op", "OP"), ("skirt", "SK"), ("sk", "SK"),
    ("连衣裙", "OP"), ("半裙", "SK"), ("套装", "SET"),
)
_GENERIC_TITLE_WORDS = {
    "the", "and", "with", "for", "dress", "skirt", "blouse", "jsk", "op",
    "set", "series", "release", "build", "order", "2023", "2024", "2025", "2026",
}
_PRODUCT_RE = re.compile(
    r"\b(?:[A-Za-z]{1,4}\d{2,}[A-Za-z0-9-]{0,12}|\d{2,}[A-Za-z]\d[A-Za-z0-9-]{0,12})\b"
)

_LATIN_TOKEN = re.compile(r"[a-z0-9]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class Brand:
    id: str
    name: str
    region: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesAlias:
    cn: str
    en: str
    brand_id: str


@dataclass(frozen=True, slots=True)
class ArchiveItem:
    title: str = ""
    brand: str = ""
    category: str = ""
    release_year: str = ""
    original_price: str = ""
    colors: tuple[str, ...] = ()
    url: str = ""
    product_number: str = ""
    alias: str = ""


@dataclass(frozen=True, slots=True)
class DressQuery:
    question: str = ""
    brand: str = ""
    brand_id: str = ""
    region: str = "UNKNOWN"
    series_en: str = ""
    series_cn: str = ""
    product_number: str = ""
    lolibrary_query: str = ""
    social_query: str = ""
    confidence: str = "low"
    source: str = "none"
    is_specific: bool = False
    verified: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "brand": self.brand,
            "brand_id": self.brand_id,
            "region": self.region,
            "series_en": self.series_en,
            "series_cn": self.series_cn,
            "product_number": self.product_number,
            "lolibrary_query": self.lolibrary_query,
            "social_query": self.social_query,
            "confidence": self.confidence,
            "source": self.source,
            "is_specific": self.is_specific,
            "verified": self.verified,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> DressQuery | None:
        if not isinstance(data, dict) or not data.get("source"):
            return None
        return cls(
            question=str(data.get("question") or ""),
            brand=str(data.get("brand") or ""),
            brand_id=str(data.get("brand_id") or ""),
            region=str(data.get("region") or "UNKNOWN"),
            series_en=str(data.get("series_en") or ""),
            series_cn=str(data.get("series_cn") or ""),
            product_number=str(data.get("product_number") or ""),
            lolibrary_query=str(data.get("lolibrary_query") or ""),
            social_query=str(data.get("social_query") or ""),
            confidence=str(data.get("confidence") or "low"),
            source=str(data.get("source") or "none"),
            is_specific=bool(data.get("is_specific")),
            verified=bool(data.get("verified")),
            notes=tuple(str(item) for item in (data.get("notes") or [])),
        )


# --------------------------------------------------------------------- 数据加载


def load_brands(app: AppSettings | None = None) -> list[Brand]:
    """读品牌/地区注册表；文件缺失或坏掉返回空表，不阻断问答。"""
    app = app or default_settings
    path = app.brands_path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("品牌表读取失败：%s", exc)
        return []
    entries = raw.get("brands") if isinstance(raw, dict) else raw
    brands: list[Brand] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases = tuple(
            str(alias).strip() for alias in (item.get("aliases") or []) if str(alias).strip()
        )
        brands.append(
            Brand(
                id=str(item.get("id") or name).strip(),
                name=name,
                region=str(item.get("region") or "UNKNOWN").strip().upper(),
                aliases=aliases,
            )
        )
    return brands


_learned_lock = threading.Lock()


def _read_alias_file(path) -> list[dict]:
    """读一个别名文件；文件缺失/坏掉返回空表，不阻断问答。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("系列别名表读取失败 %s：%s", path, exc)
        return []
    entries = raw.get("aliases") if isinstance(raw, dict) else raw
    return [item for item in entries or [] if isinstance(item, dict)]


def load_series_aliases(app: AppSettings | None = None) -> list[SeriesAlias]:
    """读系列级中文译名表：人工表优先，其次模型核实后学到的运行时缓存。

    同一个中文名以人工表为准；学到的条目只在人工表没有时生效。
    """
    app = app or default_settings
    aliases: list[SeriesAlias] = []
    seen: set[str] = set()
    for path in (app.dress_aliases_path, app.learned_aliases_path):
        for item in _read_alias_file(path):
            cn = str(item.get("cn") or "").strip()
            en = str(item.get("en") or "").strip()
            if not cn or not en or cn in seen:
                continue
            seen.add(cn)
            aliases.append(
                SeriesAlias(cn=cn, en=en, brand_id=str(item.get("brand") or "").strip())
            )
    aliases.sort(key=lambda item: len(item.cn), reverse=True)
    return aliases


def load_glossary(app: AppSettings | None = None) -> list[dict]:
    """读中文意象词表（甜心→sweetheart、拿铁→latte…）。"""
    app = app or default_settings
    try:
        raw = json.loads(app.dress_glossary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("中文意象词表读取失败：%s", exc)
        return []
    entries = raw.get("terms") if isinstance(raw, dict) else raw
    terms: list[dict] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        cn = str(item.get("cn") or "").strip()
        words = [
            str(word).strip()
            for word in (item.get("en") or [])
            if str(word).strip()
        ]
        if cn and words:
            terms.append({"cn": cn, "en": words})
    return terms


def remember_series_alias(
    cn: str,
    en: str,
    brand_id: str,
    ctx: RecallContext,
    *,
    verified_url: str = "",
) -> bool:
    """把「模型识别 + Lolibrary 核实」的译名写进运行时缓存，下次零成本命中。

    只接受能映射到已知品牌的长名；写盘用临时文件替换，避免半截 JSON。
    """
    cn = (cn or "").strip()
    en = (en or "").strip()
    brand_id = (brand_id or "").strip()
    if len(cn) < 2 or len(en) < 3 or not brand_id:
        return False
    brand = _brand_by_id(load_brands(ctx.settings), brand_id)
    if brand is None:
        brand = _brand_by_name(load_brands(ctx.settings), brand_id)
    if brand is None:
        return False

    path = ctx.settings.learned_aliases_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _learned_lock:
        entries = _read_alias_file(path)
        stamp = date.today().isoformat()
        updated = False
        for item in entries:
            if str(item.get("cn") or "").strip() != cn:
                continue
            item.update(
                {"en": en, "brand": brand.id, "verified_url": verified_url, "verified_at": stamp}
            )
            updated = True
            break
        if not updated:
            entries.append(
                {
                    "cn": cn,
                    "en": en,
                    "brand": brand.id,
                    "verified_url": verified_url,
                    "verified_at": stamp,
                }
            )
        payload = {"aliases": entries[-500:]}
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temp.replace(path)
    return True


# --------------------------------------------------------------------- 识别


def _alias_hit(alias: str, text: str) -> bool:
    alias = (alias or "").strip().lower()
    text = (text or "").lower()
    if not alias:
        return False
    if alias.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def _has_context(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in (*_DRESS_MARKERS, *FIELD_MARKERS))


def _match_brand(question: str, brands: list[Brand]) -> tuple[Brand | None, str]:
    """匹配品牌；短别名要求问题里有裙子/信息语境，多品牌命中不猜。"""
    lowered = question.lower()
    hits: list[tuple[int, Brand, str]] = []
    for brand in brands:
        for alias in (brand.name, *brand.aliases):
            if not _alias_hit(alias, lowered):
                continue
            if len(alias.strip()) <= 2 and not _has_context(question):
                continue
            hits.append((len(alias.strip()), brand, alias))
    if not hits:
        return None, ""
    hits.sort(key=lambda item: item[0], reverse=True)
    brands_hit = {item[1].id: item[1] for item in hits}
    if len(brands_hit) > 1:
        regions = {brand.region for brand in brands_hit.values()}
        if len(regions) == 1:
            return hits[0][1], "多个品牌命中，按最长别名处理"
        names = "、".join(brand.name for brand in brands_hit.values())
        return None, f"品牌冲突：{names}"
    return hits[0][1], ""


def _brand_by_id(brands: list[Brand], brand_id: str) -> Brand | None:
    for brand in brands:
        if brand.id == brand_id:
            return brand
    return None


def _brand_by_name(brands: list[Brand], name: str) -> Brand | None:
    target = (name or "").strip().lower()
    if not target:
        return None
    for brand in brands:
        if target in {brand.name.lower(), *(alias.lower() for alias in brand.aliases)}:
            return brand
    return None


def _region_of_name(brands: list[Brand], name: str) -> tuple[str, str, str]:
    """返回 ``(品牌全称, 地区, 品牌 id)``；档案里的泛化品牌按标签处理。"""
    brand = _brand_by_name(brands, name)
    if brand is not None:
        return brand.name, brand.region, brand.id
    lowered = (name or "").strip().lower()
    if lowered == "chinese indie brand":
        return name, "CN", ""
    if lowered == "indie brand":
        return name, "UNKNOWN", ""
    return name, "UNKNOWN", ""


def _one_char_apart(text: str, target: str) -> bool:
    """等长且只差一个字：用于容忍「皇/黄」这类同音、形近输入。"""
    return len(text) == len(target) and sum(a != b for a, b in zip(text, target, strict=True)) == 1


def _match_series(question: str, aliases: list[SeriesAlias]) -> tuple[SeriesAlias | None, str]:
    for alias in aliases:
        if alias.cn in question or _alias_hit(alias.en, question):
            return alias, ""
    # 中文社区叫法经常靠口耳相传，错一个字很常见。只对 >=4 字的别名做一字容错，
    # 避免把「四时花」这种短词匹配到别的三字短语上。
    for alias in aliases:
        target = alias.cn
        if len(target) < 4:
            continue
        for start in range(0, len(question) - len(target) + 1):
            window = question[start:start + len(target)]
            if _one_char_apart(window, target):
                return alias, f"按近似中文名「{window}」匹配"
    return None, ""


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").lower())


def _title_tokens(title: str) -> set[str]:
    tokens = {token for token in _LATIN_TOKEN.findall((title or "").lower()) if len(token) >= 3}
    return {token for token in tokens if token not in _GENERIC_TITLE_WORDS}


def _normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _record_aliases(record: dict) -> list[str]:
    raw = record.get("aliases") or []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw if str(item).strip()]


def _match_record(question: str, records: list[dict]) -> tuple[dict | None, str]:
    """在本地档案里找具体裙子：货号 → 别名 → 标题。"""
    compact = _normalize_number(question)
    for record in records:
        number = _normalize_number(str(record.get("product_number") or ""))
        if number and len(number) >= 4 and number in compact:
            return record, "product_number"

    lowered = question.lower()
    best: tuple[int, dict] | None = None
    for record in records:
        for alias in _record_aliases(record):
            if (
                len(alias.strip()) >= 2
                and _alias_hit(alias, lowered)
                and (best is None or len(alias) > best[0])
            ):
                best = (len(alias), record)
    if best is not None:
        return best[1], "alias"

    query_tokens = _title_tokens(question)
    best = None
    for record in records:
        title = str(record.get("title") or "")
        normalized = _normalize_title(title)
        if normalized and len(normalized) >= 6 and normalized in _normalize_title(question):
            return record, "title"
        overlap = query_tokens & _title_tokens(title)
        if len(overlap) >= 2 and (best is None or len(overlap) > best[0]):
            best = (len(overlap), record)
    if best is not None:
        return best[1], "title"
    return None, ""


def _category_hint(question: str) -> str:
    lowered = question.lower()
    for marker, category in _CATEGORY_HINTS:
        if marker in lowered:
            return category
    return ""


def _field_term(question: str) -> str:
    lowered = question.lower()
    if "预约" in lowered or "定金" in lowered:
        return "预约价"
    if "价格" in lowered or "多少钱" in lowered or "原价" in lowered or "多少" in lowered:
        return "价格"
    if "配色" in lowered or "颜色" in lowered:
        return "配色"
    if "发售" in lowered or "上市" in lowered or "出荷" in lowered:
        return "发售"
    return ""


def _with_queries(query: DressQuery) -> DressQuery:
    category = _category_hint(query.question)
    # 有具体系列名时不要再带品牌：Lolibrary 通道的「品牌命中即入选」会把
    # 该品牌下所有条目都带出来，反而盖过真正的系列。
    base = query.series_en or query.brand
    lolibrary_query = " ".join(part for part in (base, category) if part).strip()

    social_base = query.series_cn or query.series_en or query.brand
    social_query = " ".join(
        part for part in (social_base, category, _field_term(query.question)) if part
    ).strip()
    return DressQuery(
        question=query.question,
        brand=query.brand,
        brand_id=query.brand_id,
        region=query.region,
        series_en=query.series_en,
        series_cn=query.series_cn,
        product_number=query.product_number,
        lolibrary_query=lolibrary_query,
        social_query=social_query or query.question,
        confidence=query.confidence,
        source=query.source,
        is_specific=query.is_specific,
        verified=query.verified,
        notes=query.notes,
    )


_GENERIC_QUESTION_WORDS = (
    "怎么", "怎样", "如何", "一般", "大概", "现在", "这个", "那个", "一下",
    "可以", "能", "吗", "呢", "的", "算", "价", "钱", "呀", "啊", "吧",
    "和", "与", "都", "想", "知道", "看", "好看", "值得", "买", "选", "挑",
    "比较", "区别", "测评", "口碑", "评价", "种草", "避雷", "攻略", "实拍",
    "开箱", "返图", "推荐", "尺码", "洗护", "搭配", "价格", "预约", "现货",
    "新作", "上新", "发售", "多少", "今天", "明天", "昨天", "谢谢", "你好",
    "在吗", "哈哈", "累了", "好累", "晚安", "早安", "经典", "款", "最近",
    "什么", "有", "哪些", "求", "喜欢", "想要", "需要",
)

# 必须先剥长词再剥短词：否则「价格」会先被「价」拆掉，留下一个「格」。
_STRIP_MARKERS = tuple(
    sorted(
        {*_GENERIC_QUESTION_WORDS, *FIELD_MARKERS, *CARE_MARKERS, *_DRESS_MARKERS},
        key=len,
        reverse=True,
    )
)


def _strip_markers(text: str) -> str:
    for marker in _STRIP_MARKERS:
        if marker:
            text = text.replace(marker, " ")
    return text


def _name_chars(text: str) -> int:
    """去掉品牌、意图词和泛化问法后，还剩多少个「像名字」的字符。"""
    cleaned = _strip_markers((text or "").lower())
    cjk = "".join(_CJK.findall(cleaned))
    latin = re.findall(r"[a-z0-9]{3,}", cleaned)
    return len(cjk) + len(latin)


def _looks_like_bare_name(text: str) -> bool:
    """用户在只发一个名字（没有「价格/发售」等意图词）时，也值得让模型认一次。"""
    stripped = (text or "").strip()
    if not 3 <= len(stripped) <= 24:
        return False
    lowered = stripped.lower()
    if any(marker in lowered for marker in (*FIELD_MARKERS, *CARE_MARKERS, *_DRESS_MARKERS)):
        return False
    return _name_chars(stripped) >= 3


def _brand_name_chars(text: str, brand: Brand) -> int:
    lowered = (text or "").lower()
    for name in (brand.name, *brand.aliases):
        if name:
            lowered = lowered.replace(name.lower(), " ")
    return _name_chars(lowered)


def _name_text(question: str, brands: list[Brand] | None = None) -> str:
    """从问题里剥出中文名字（去掉品牌、意图词、泛化问法）。"""
    lowered = (question or "").lower()
    for brand in brands or []:
        for name in (brand.name, *brand.aliases):
            if name:
                lowered = lowered.replace(name.lower(), " ")
    return "".join(_CJK.findall(_strip_markers(lowered)))


def split_keywords(name: str, terms: list[dict]) -> tuple[list[str], str]:
    """按最长词优先，把中文名拆成英文意象词；返回 ``(英文词, 未识别中文)``。"""
    ordered = sorted(terms, key=lambda item: len(item["cn"]), reverse=True)
    words: list[str] = []
    unmatched: list[str] = []
    index = 0
    while index < len(name):
        for term in ordered:
            if name.startswith(term["cn"], index):
                words.append(term["en"][0])
                index += len(term["cn"])
                break
        else:
            unmatched.append(name[index])
            index += 1
    return words, "".join(unmatched)


def glossary_candidates(name: str, terms: list[dict]) -> list[str]:
    """把中文名拼成 1-2 个英文候选（原序/倒序）；未识别字太多就不猜。"""
    words, unmatched = split_keywords(name, terms)
    if not words or len(unmatched) > 2:
        return []
    candidates = [" ".join(words)]
    if len(words) > 1:
        candidates.append(" ".join(reversed(words)))
    result: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _verified_from_item(
    current: DressQuery,
    item: ArchiveItem,
    *,
    name: str,
    series_en: str,
    source: str,
    ctx: RecallContext,
) -> dict:
    brand_name = item.brand or current.brand
    canonical, region, brand_id = _region_of_name(load_brands(ctx.settings), brand_name)
    updated = replace(
        current,
        brand=canonical or brand_name or current.brand,
        brand_id=brand_id or current.brand_id,
        region=region if region != "UNKNOWN" else current.region,
        series_en=series_en,
        series_cn=name or current.series_cn,
        product_number=item.product_number or current.product_number,
        confidence="high",
        source=source,
        is_specific=True,
        verified=True,
        notes=current.notes + ("已用 Lolibrary 档案核实",),
    )
    if name and brand_id:
        remember_series_alias(name, series_en, brand_id, ctx, verified_url=item.url)
    return _with_queries(updated).to_dict()


def resolve_by_glossary(
    query: dict,
    ctx: RecallContext,
    *,
    fetcher=None,
) -> tuple[dict, dict]:
    """把中文名拆成英文意象词，去 Lolibrary 搜；命中才返回新 query。"""
    current = DressQuery.from_dict(query)
    if current is None or current.series_en or not ctx.settings.lolibrary_online_enabled:
        return query, {}
    name = _name_text(current.question, load_brands(ctx.settings))
    candidates = glossary_candidates(name, load_glossary(ctx.settings))
    if not candidates:
        return query, {}
    error = ""
    for candidate in candidates:
        items, error = lolibrary_online.lookup(
            candidate,
            brand=current.brand,
            fetcher=fetcher,
            max_items=2,
        )
        probe = replace(current, series_en=candidate)
        for item in items:
            if not _archive_matches(probe, item):
                continue
            updated = _verified_from_item(
                current, item, name=name, series_en=candidate, source="glossary_verified", ctx=ctx
            )
            return updated, {
                "via": "glossary",
                "candidates": candidates,
                "matched_title": item.title,
                "matched_url": item.url,
                "brand": item.brand,
            }
    return query, {"via": "glossary", "candidates": candidates, "error": error or "未命中"}


_LATIN_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9'’&-]{1,}(?:\s+(?:[a-z]{2,5}\s+)?[A-Z][A-Za-z0-9'’&-]{1,}){1,4})\b"
)
_PHRASE_STOPWORDS = {
    "lolita", "jsk", "op", "sk", "baby", "angelic", "pretty", "the", "and",
    "sweet", "dream", "dreamy", "latte", "dress", "skirt", "blouse", "new",
}


def _latin_phrases(text: str) -> list[str]:
    """从搜索结果里抽可能的英文系列名；过滤掉 Lolita/JSK 这类泛词。"""
    phrases: list[str] = []
    for match in _LATIN_PHRASE_RE.finditer(text or ""):
        phrase = re.sub(r"\s+", " ", match.group(1)).strip()
        words = phrase.split()
        if len(words) < 2 or len(phrase) < 8:
            continue
        if all(word.lower() in _PHRASE_STOPWORDS for word in words):
            continue
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def resolve_by_web(
    query: dict,
    ctx: RecallContext,
    *,
    fetcher=None,
) -> tuple[dict, dict]:
    """用中文名搜网页/小红书，从结果里抽英文系列名，再回 Lolibrary 校验。"""
    current = DressQuery.from_dict(query)
    if current is None or current.series_en or not ctx.settings.lolibrary_online_enabled:
        return query, {}
    if not (ctx.settings.search_enabled or ctx.settings.xhs_enabled):
        return query, {}
    name = _name_text(current.question, load_brands(ctx.settings))
    if len(name) < 2:
        return query, {}
    queries = [name, f"{name} lolita"][:2]
    results: list = []
    errors: list[str] = []
    if ctx.settings.search_enabled:
        from app.services import web_search

        for text in queries:
            try:
                results.extend(web_search.search(text, ctx.settings, max_results=3))
            except Exception as exc:  # noqa: BLE001 - 搜索失败继续走模型
                errors.append(f"{type(exc).__name__}")
    if ctx.settings.xhs_enabled:
        from app.services import xhs_search

        for text in queries:
            try:
                results.extend(xhs_search.search(text, ctx.settings, max_results=3))
            except Exception as exc:  # noqa: BLE001 - 小红书失败不阻断
                errors.append(f"xhs:{type(exc).__name__}")

    text = "\n".join(
        f"{getattr(item, 'title', '')} {getattr(item, 'snippet', '') or getattr(item, 'content', '')}"
        for item in results
    )
    candidates = _latin_phrases(text)
    if not candidates:
        return query, {
            "via": "web",
            "queries": queries,
            "candidates": [],
            "error": "搜索结果里没有可用的英文系列名" if results else "搜索没有返回结果",
        } | ({"errors": errors} if errors else {})
    for candidate in candidates[:4]:
        items, error = lolibrary_online.lookup(
            candidate,
            brand=current.brand,
            fetcher=fetcher,
            max_items=1,
        )
        probe = replace(current, series_en=candidate)
        for item in items:
            if not _archive_matches(probe, item):
                continue
            updated = _verified_from_item(
                current, item, name=name, series_en=candidate, source="search_verified", ctx=ctx
            )
            return updated, {
                "via": "web",
                "queries": queries,
                "candidates": candidates,
                "matched_title": item.title,
                "matched_url": item.url,
                "brand": item.brand,
            }
        if error:
            errors.append(error)
    return query, {
        "via": "web",
        "queries": queries,
        "candidates": candidates,
        "error": "候选都没在 Lolibrary 命中",
        "errors": errors,
    }


def resolve_with_search(
    query: dict,
    ctx: RecallContext,
    *,
    fetcher=None,
) -> tuple[dict, list[dict]]:
    """未识别名字的检索兜底：先拆词查 Lolibrary，再走网页/社媒反查。"""
    updated, glossary_trace = resolve_by_glossary(query, ctx, fetcher=fetcher)
    traces = [glossary_trace] if glossary_trace else []
    if updated.get("series_en"):
        return updated, traces
    updated, web_trace = resolve_by_web(updated, ctx, fetcher=fetcher)
    if web_trace:
        traces.append(web_trace)
    return updated, traces


def _looks_like_dress_question(text: str) -> bool:
    """本地没认出任何实体时，判断是否值得让模型兜底一次。

    条件：有信息字段意图（价格/发售/配色/品牌…），且问题里还有裙子名词，
    或去掉意图词/泛化问法后仍剩一个 ≥3 字的中文片段（像系列名）。
    「预约价怎么算」会被剥空，不会白调模型；「星月夜之梦价格」会保留下来。
    """
    lowered = (text or "").lower()
    if not any(marker in lowered for marker in FIELD_MARKERS):
        return False
    if any(marker in lowered for marker in CARE_MARKERS):
        return False
    if any(marker in lowered for marker in _DRESS_MARKERS):
        return True
    stripped = _strip_markers(lowered)
    cjk_left = "".join(_CJK.findall(stripped))
    return len(cjk_left) >= 3


def resolve_dress(
    question: str,
    ctx: RecallContext,
    *,
    brands: list[Brand] | None = None,
    aliases: list[SeriesAlias] | None = None,
    records: list[dict] | None = None,
) -> DressQuery | None:
    """离线识别一条裙子的品牌/地区；不该触发时返回 None（不调模型）。"""
    text = (question or "").strip()
    if not text:
        return None
    brands = load_brands(ctx.settings) if brands is None else brands
    aliases = load_series_aliases(ctx.settings) if aliases is None else aliases
    records = channels.load_catalog(ctx) if records is None else records

    record, matched_by = _match_record(text, records)
    series, series_note = _match_series(text, aliases)
    brand, brand_note = _match_brand(text, brands)

    notes: list[str] = [note for note in (brand_note, series_note) if note]
    record_brand_name = str(record.get("brand") or "").strip() if record else ""
    record_brand_name, record_region, record_brand_id = _region_of_name(brands, record_brand_name)

    series_brand = _brand_by_id(brands, series.brand_id) if series else None
    series_brand_name = series_brand.name if series_brand else ""
    series_region = series_brand.region if series_brand else "UNKNOWN"

    candidates: list[tuple[str, str, str]] = []
    if record_brand_name:
        candidates.append((record_brand_name, record_region, record_brand_id))
    if series_brand_name:
        candidates.append((series_brand_name, series_region, series.brand_id))
    if brand is not None:
        candidates.append((brand.name, brand.region, brand.id))

    distinct = {item[0] for item in candidates if item[0]}
    regions = {item[1] for item in candidates if item[1] and item[1] != "UNKNOWN"}
    if len(distinct) > 1 and len(regions) > 1:
        notes.append("品牌来源冲突：" + "、".join(sorted(distinct)))
    if len(regions) > 1:
        final_brand, final_region, final_id = "", "UNKNOWN", ""
    elif record_brand_name and record_region != "UNKNOWN":
        final_brand, final_region, final_id = record_brand_name, record_region, record_brand_id
    elif series_brand_name:
        final_brand, final_region, final_id = series_brand_name, series_region, series.brand_id
    elif brand is not None:
        final_brand, final_region, final_id = brand.name, brand.region, brand.id
    elif record_brand_name:
        final_brand, final_region, final_id = record_brand_name, record_region, record_brand_id
    else:
        final_brand, final_region, final_id = "", "UNKNOWN", ""

    series_en = str(record.get("title") or "") if record else (series.en if series else "")
    series_cn = series.cn if series else ""
    product_number = str(record.get("product_number") or "") if record else ""
    if not product_number:
        match = _PRODUCT_RE.search(text)
        product_number = match.group(0) if match else ""

    is_specific = bool(series_en or product_number)
    has_entity = bool(final_brand or series_en or product_number or record)
    if not has_entity:
        if _looks_like_dress_question(text) or _looks_like_bare_name(text):
            pending = DressQuery(
                question=text,
                source="pending_model",
                region="UNKNOWN",
                confidence="low",
                is_specific=False,
                notes=("本地没有识别出品牌/系列，交给模型兜底",),
            )
            return _with_queries(pending)
        return None

    lowered = text.lower()
    field_intent = any(marker in lowered for marker in FIELD_MARKERS)
    care_intent = any(marker in lowered for marker in CARE_MARKERS)
    if not is_specific and not field_intent:
        # 只说了品牌 + 一个像系列名的片段（「Baby 秋日私语」）：交给模型认系列；
        # 「Baby 的裙子」「AP 有什么经典款」剥完品牌和泛化词就空了，不会误触发。
        if brand is not None and _brand_name_chars(text, brand) >= 3:
            pending = DressQuery(
                question=text,
                brand=brand.name,
                brand_id=brand.id,
                region=brand.region,
                source="pending_model",
                confidence="low",
                is_specific=False,
                notes=("本地只识别到品牌，具体系列交给模型兜底",),
            )
            return _with_queries(pending)
        return None
    if care_intent and not field_intent:
        return None

    source = "none"
    if record:
        source = f"catalog_{matched_by}"
    elif series:
        source = "series_alias"
    elif brand is not None:
        source = "brand_alias"
    confidence = "high" if record or series else ("medium" if brand is not None else "low")
    if final_region == "UNKNOWN":
        confidence = "low" if confidence == "high" else confidence

    query = DressQuery(
        question=text,
        brand=final_brand,
        brand_id=final_id,
        region=final_region,
        series_en=series_en,
        series_cn=series_cn,
        product_number=product_number,
        confidence=confidence,
        source=source,
        is_specific=is_specific,
        verified=bool(record or series),
        notes=tuple(notes),
    )
    return _with_queries(query)


def merge_model_hint(query: dict, hint: dict, ctx: RecallContext) -> dict:
    """把模型兜底结果合并进离线识别；离线命中优先，低置信度/未知品牌丢弃。"""
    current = DressQuery.from_dict(query)
    if current is None or not isinstance(hint, dict):
        return query
    confidence = str(hint.get("confidence") or "").strip().lower()
    if confidence not in ("high", "medium"):
        return query

    brands = load_brands(ctx.settings)
    hint_brand = _brand_by_name(brands, str(hint.get("brand") or ""))
    hint_region = str(hint.get("region") or "").strip().upper()
    hint_series = str(hint.get("series") or "").strip()
    hint_series_cn = str(hint.get("series_cn") or "").strip()

    notes = list(current.notes)
    brand = current.brand
    brand_id = current.brand_id
    region = current.region
    series_en = current.series_en
    series_cn = current.series_cn
    model_used = False

    hint_brand_name = str(hint.get("brand") or "").strip()
    brand_conflict = bool(
        hint_brand_name
        and brand
        and (hint_brand is None or hint_brand.name.lower() != brand.lower())
    )

    if brand_conflict:
        notes.append(f"模型品牌 {hint_brand_name} 与注册表 {brand} 不一致，按注册表")
    elif hint_brand is not None and not brand:
        brand, brand_id, region = hint_brand.name, hint_brand.id, hint_brand.region
        model_used = True
    elif not hint_brand and hint_brand_name and brand:
        notes.append(f"模型品牌 {hint_brand_name} 不在注册表，忽略")

    if (
        hint_region in ("JP", "CN", "OTHER")
        and (region == "UNKNOWN" or not region)
        and (brand or series_en)
    ):
        region = hint_region
        model_used = True
    # 品牌对不上时不采用它给的系列名：那可能是另一条裙子的名字。
    if hint_series and not series_en and not brand_conflict and brand:
        series_en = hint_series
        model_used = True
    if hint_series_cn and not series_cn:
        series_cn = hint_series_cn
        model_used = True

    source = current.source
    if model_used and source in ("pending_model", "brand_alias"):
        source = "model"
        notes.append("模型识别，待 Lolibrary 档案核实")
    confidence = "medium" if source == "model" else current.confidence

    merged = DressQuery(
        question=current.question,
        brand=brand,
        brand_id=brand_id,
        region=region,
        series_en=series_en,
        series_cn=series_cn,
        product_number=current.product_number,
        confidence=confidence,
        source=source,
        is_specific=bool(series_en or current.product_number),
        verified=current.verified,
        notes=tuple(notes),
    )
    return _with_queries(merged).to_dict()


def dress_channels(query: dict, intent_channels: list[str]) -> list[str]:
    """按地区决定补查通道；JP 只走核心 Lolibrary，CN 强制 xhs+weibo。"""
    region = str((query or {}).get("region") or "UNKNOWN")
    if region == "JP":
        wanted: list[str] = []
    elif region == "CN":
        wanted = list(SOCIAL_CHANNELS)
    else:
        wanted = [*SOCIAL_CHANNELS, *(intent_channels or [])]
    result: list[str] = []
    for name in wanted:
        if name not in result:
            result.append(name)
    return result


# --------------------------------------------------------------------- 档案取数


def _record_item(record: dict) -> ArchiveItem:
    return ArchiveItem(
        title=str(record.get("title") or ""),
        brand=str(record.get("brand") or ""),
        category=str(record.get("category") or ""),
        release_year=str(record.get("release_year") or ""),
        original_price=str(record.get("original_price") or ""),
        colors=tuple(str(item) for item in (record.get("colors") or [])),
        url=str(record.get("url") or ""),
        product_number=str(record.get("product_number") or ""),
        alias=" ".join(_record_aliases(record)),
    )


def _detail_item(detail: lolibrary_online.ArchiveDetail) -> ArchiveItem:
    return ArchiveItem(
        title=detail.title,
        brand=detail.brand,
        category=detail.category,
        release_year=detail.release_year,
        original_price=detail.original_price,
        colors=detail.colors,
        url=detail.url,
        product_number=detail.product_number,
        alias=detail.alias,
    )


def _find_archive_record(query: DressQuery, records: list[dict]) -> dict | None:
    number = _normalize_number(query.product_number)
    if number:
        for record in records:
            if _normalize_number(str(record.get("product_number") or "")) == number:
                return record
    normalized = _normalize_title(query.series_en)
    if not normalized:
        return None
    brand = query.brand.lower()
    best: tuple[int, dict] | None = None
    for record in records:
        title = _normalize_title(str(record.get("title") or ""))
        if not title:
            continue
        score = 0
        if title == normalized:
            score = 100
        elif normalized in title or title in normalized:
            score = 60
        else:
            token_hits = len(_title_tokens(query.series_en) & _title_tokens(str(record.get("title") or "")))
            if token_hits >= 2:
                score = token_hits * 10
        if score and brand and brand in str(record.get("brand") or "").lower():
            score += 5
        if score and (best is None or score > best[0]):
            best = (score, record)
    return best[1] if best else None


def lookup_archive(
    query: dict,
    ctx: RecallContext,
    *,
    fetcher=None,
    online: bool | None = None,
) -> tuple[list[ArchiveItem], str]:
    """本地档案优先；字段不全或没命中时在线补详情。返回 ``(条目, 错误)``。"""
    current = DressQuery.from_dict(query)
    if current is None or not current.is_specific:
        return [], ""
    online_enabled = ctx.settings.lolibrary_online_enabled if online is None else online
    records = channels.load_catalog(ctx)
    record = _find_archive_record(current, records)
    if record is not None:
        item = _record_item(record)
        needs_detail = not (item.release_year and item.original_price and item.colors)
        # 国牌按需求只查小红书/微博；本地档案只提供身份，不再去 Lolibrary 详情页。
        if current.region != "CN" and needs_detail and item.url and online_enabled:
            card = lolibrary_online.SearchCard(
                url=item.url,
                title=item.title,
                brand=item.brand,
                category=item.category,
                product_number=item.product_number,
                alias=item.alias,
            )
            detail, error = lolibrary_online.fetch_detail(item.url, card, fetcher=fetcher)
            if detail is not None:
                item = ArchiveItem(
                    title=item.title or detail.title,
                    brand=item.brand or detail.brand,
                    category=item.category or detail.category,
                    release_year=detail.release_year or item.release_year,
                    original_price=detail.original_price or item.original_price,
                    colors=detail.colors or item.colors,
                    url=item.url,
                    product_number=detail.product_number or item.product_number,
                    alias=item.alias or detail.alias,
                )
            elif error:
                return [item], error
        return [item], ""

    if current.region == "CN":
        return [], ""
    if not online_enabled:
        return [], "在线档案已关闭"
    query_text = current.series_en or current.product_number
    # 模型给的品牌可能认错：先不按品牌过滤，让档案自己告诉我们它是谁；
    # 后面 build_profile 再用标题 token 校验，避免被错品牌带偏。
    brand_filter = "" if current.source == "model" else current.brand
    details, error = lolibrary_online.lookup(
        query_text,
        brand=brand_filter,
        fetcher=fetcher,
    )
    return [_detail_item(detail) for detail in details], error


# --------------------------------------------------------------------- 社媒字段抽取

_MASK = re.compile(
    r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*[日号])?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"
    r"|\d+(?:\.\d+)?\s*(?:cm|厘米|mm|码|折|成新|期|天)"
)
_PRICE_PATTERNS = (
    re.compile(r"(?:CN¥|¥|￥|RMB|人民币)\s*([\d,]{2,6}(?:\.\d+)?)", re.I),
    re.compile(r"([\d,]{2,6}(?:\.\d+)?)\s*元"),
    re.compile(r"(定金|订金|全款|尾款|预约价|现货价|到手价)\s*[:：]?\s*[¥￥]?\s*([\d,]{2,6})"),
)
_DATE_CN = re.compile(r"((?:19|20)\d{2})\s*年(?:\s*(\d{1,2})\s*月)?")
_DATE_ISO = re.compile(r"((?:19|20)\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?")
_DATE_CONTEXT = ("发售", "开团", "预约", "再贩", "出荷", "上架", "尾款", "定金")
_STYLE_RE = re.compile(r"\b(OP|JSK|SK|SKIRT|KC|SET|DRESS)\b", re.I)
_STYLE_CN = (("连衣裙", "OP"), ("背心裙", "JSK"), ("半裙", "SK"), ("套装", "SET"))
_CN_COLORS = (
    "樱花粉", "浅粉", "深粉", "粉", "藏青", "浅蓝", "深蓝", "蓝", "黑", "米白",
    "生成", "酒红", "红", "浅紫", "深紫", "紫", "墨绿", "浅绿", "绿", "灰",
    "棕", "咖", "金", "银", "香槟", "裸色", "杏色", "奶茶", "白",
)
_EN_COLORS = (
    "offwhite", "off-white", "sax blue", "sax", "pink", "white", "black",
    "ivory", "cream", "red", "wine", "burgundy", "purple", "lavender", "green",
    "mint", "gray", "grey", "brown", "gold", "silver", "blue", "navy",
    "yellow", "beige", "orange", "champagne",
)
_CN_COLOR_RE = re.compile("|".join(re.escape(word) for word in _CN_COLORS))
_EN_COLOR_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(word) for word in _EN_COLORS) + r")(?![a-z])",
    re.I,
)


def _value(candidate: dict) -> str:
    return str(candidate.get("value") or "")


def _candidates_from_evidence(
    evidence: list[dict],
    pattern: re.Pattern,
    *,
    group: int = 1,
    min_value: int | None = None,
    role: str = "price",
) -> list[dict]:
    found: list[dict] = []
    for item in evidence:
        if item.get("channel") not in SOCIAL_CHANNELS:
            continue
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        masked = _MASK.sub(" ", text)
        for match in pattern.finditer(masked):
            raw = match.group(0)
            try:
                number = int(str(match.group(group)).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if min_value is not None and not min_value <= number <= 50000:
                continue
            prefix = masked[max(0, match.start() - 8): match.start()]
            actual_role = role
            if any(word in prefix for word in ("定金", "订金", "尾款")):
                actual_role = "deposit"
            found.append({
                "value": raw.strip(),
                "amount": number,
                "role": actual_role,
                "raw": masked[max(0, match.start() - 20): match.end() + 20].strip(),
                "channel": item.get("channel"),
                "ref": item.get("url") or item.get("ref") or "",
            })
    return found


def _deposit_roles(evidence: list[dict]) -> list[dict]:
    """把「定金/尾款」单独标出来：它和全款不是同一个字段，不能算冲突。"""
    found: list[dict] = []
    pattern = _PRICE_PATTERNS[2]
    for item in evidence:
        if item.get("channel") not in SOCIAL_CHANNELS:
            continue
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        masked = _MASK.sub(" ", text)
        for match in pattern.finditer(masked):
            label = match.group(1)
            try:
                number = int(str(match.group(2)).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if not 20 <= number <= 50000:
                continue
            role = "deposit" if label in ("定金", "订金", "尾款") else "price"
            found.append({
                "value": f"{label} {number}",
                "amount": number,
                "role": role,
                "raw": masked[max(0, match.start() - 20): match.end() + 20].strip(),
                "channel": item.get("channel"),
                "ref": item.get("url") or item.get("ref") or "",
            })
    return found


def _dedupe_prices(candidates: list[dict]) -> list[dict]:
    """同一笔钱可能被多条正则同时命中（"¥398" 和 "预约价 ¥398"）；按金额去重。"""
    best: dict[tuple[str, int], dict] = {}
    for candidate in candidates:
        key = (str(candidate.get("role") or "price"), int(candidate.get("amount") or 0))
        current = best.get(key)
        if current is None or len(_value(candidate)) > len(_value(current)):
            best[key] = candidate
    return list(best.values())


def _social_fields(evidence: list[dict]) -> dict[str, list[dict]]:
    prices: list[dict] = []
    for pattern in _PRICE_PATTERNS[:2]:
        prices.extend(
            _candidates_from_evidence(evidence, pattern, group=1, min_value=20, role="price")
        )
    prices.extend(_deposit_roles(evidence))
    prices = _dedupe_prices(prices)
    releases: list[dict] = []
    for item in evidence:
        if item.get("channel") not in SOCIAL_CHANNELS:
            continue
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        for pattern in (_DATE_CN, _DATE_ISO):
            for match in pattern.finditer(text):
                window = text[max(0, match.start() - 24): match.end() + 24]
                if not any(marker in window for marker in _DATE_CONTEXT):
                    continue
                year = match.group(1)
                month = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                if month and (not month.isdigit() or not 1 <= int(month) <= 12):
                    continue
                value = f"{year}-{int(month):02d}" if month else year
                releases.append({
                    "value": value,
                    "raw": match.group(0).strip(),
                    "channel": item.get("channel"),
                    "ref": item.get("url") or item.get("ref") or "",
                })

    styles: list[dict] = []
    colors: list[dict] = []
    for item in evidence:
        if item.get("channel") not in SOCIAL_CHANNELS:
            continue
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        for match in _STYLE_RE.finditer(text):
            styles.append({
                "value": match.group(1).upper(),
                "raw": match.group(0),
                "channel": item.get("channel"),
                "ref": item.get("url") or item.get("ref") or "",
            })
        for word, mapped in _STYLE_CN:
            if word in text:
                styles.append({
                    "value": mapped,
                    "raw": word,
                    "channel": item.get("channel"),
                    "ref": item.get("url") or item.get("ref") or "",
                })
        for match in _CN_COLOR_RE.finditer(text):
            colors.append({
                "value": match.group(0),
                "raw": match.group(0),
                "channel": item.get("channel"),
                "ref": item.get("url") or item.get("ref") or "",
            })
        for match in _EN_COLOR_RE.finditer(text):
            colors.append({
                "value": match.group(1).title(),
                "raw": match.group(0),
                "channel": item.get("channel"),
                "ref": item.get("url") or item.get("ref") or "",
            })

    return {
        "price": prices,
        "release": releases,
        "style": styles,
        "colors": colors,
    }


def _unique_values(candidates: list[dict]) -> list[str]:
    values: list[str] = []
    for candidate in candidates:
        value = _value(candidate)
        if value and value not in values:
            values.append(value)
    return values


# --------------------------------------------------------------------- 档案整合


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EVIDENCE_TEXT_LIMIT else text[:EVIDENCE_TEXT_LIMIT] + "…"


def _region_label(region: str) -> str:
    return {
        "JP": "日牌",
        "CN": "国牌",
        "OTHER": "其他地区",
        "UNKNOWN": "地区未识别",
    }.get(region, "地区未识别")


def _archive_matches(query: DressQuery, item: ArchiveItem) -> bool:
    """模型给的系列名和档案标题至少要有两个词重合（或整串包含）。"""
    if not item or not query.series_en:
        return False
    overlap = _title_tokens(query.series_en) & _title_tokens(item.title)
    if len(overlap) >= 2:
        return True
    wanted = _normalize_title(query.series_en)
    title = _normalize_title(item.title)
    return bool(wanted and title and (wanted in title or title in wanted))


def build_profile(
    query: dict,
    evidence: list[dict],
    ctx: RecallContext,
    *,
    fetcher=None,
    online: bool | None = None,
) -> dict:
    """整合结构化档案与一条可引用的证据；没有识别结果返回空 dict。"""
    current = DressQuery.from_dict(query)
    if current is None:
        return {}
    if current.source == "pending_model" and not (current.brand or current.series_en):
        return {}

    archive, archive_error = lookup_archive(query, ctx, fetcher=fetcher, online=online)
    archive_item = archive[0] if archive else None
    if current.source in ("model", "model_verified"):
        matched = next((item for item in archive if _archive_matches(current, item)), None)
        if matched is not None:
            brand_name = matched.brand or current.brand
            canonical, region, brand_id = _region_of_name(load_brands(ctx.settings), brand_name)
            current = replace(
                current,
                brand=canonical or brand_name or current.brand,
                brand_id=brand_id or current.brand_id,
                region=region if region != "UNKNOWN" else current.region,
                series_en=current.series_en or matched.title,
                confidence="high",
                source="model_verified",
                verified=True,
                notes=current.notes + ("已用 Lolibrary 档案核实",),
            )
            archive_item = matched
            if current.series_cn:
                remember_series_alias(
                    current.series_cn,
                    current.series_en,
                    brand_id,
                    ctx,
                    verified_url=matched.url,
                )
        else:
            archive_item = None
            current = replace(
                current,
                confidence="low",
                verified=False,
                notes=current.notes + ("模型识别，未在 Lolibrary 档案中核实",),
            )

    social = _social_fields(evidence)
    notes = list(current.notes)

    release_candidates: list[dict] = []
    price_candidates: list[dict] = []
    style_candidates: list[dict] = []
    color_candidates: list[dict] = []

    if archive_item is not None:
        if current.region in ("JP", "OTHER", "UNKNOWN"):
            if archive_item.release_year:
                release_candidates.append({
                    "value": archive_item.release_year,
                    "kind": "archive",
                    "raw": f"Released in {archive_item.release_year}",
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })
            if archive_item.original_price:
                price_candidates.append({
                    "value": archive_item.original_price,
                    "kind": "archive_original",
                    "raw": archive_item.original_price,
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })
            if archive_item.category:
                style_candidates.append({
                    "value": archive_item.category,
                    "kind": "archive",
                    "raw": archive_item.category,
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })
            for color in archive_item.colors:
                color_candidates.append({
                    "value": color,
                    "kind": "archive",
                    "raw": color,
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })
        else:
            # 国牌：档案只作身份补充（标题/品牌/品类），价格日期以社媒为准。
            if archive_item.category:
                style_candidates.append({
                    "value": archive_item.category,
                    "kind": "archive",
                    "raw": archive_item.category,
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })

    if current.region in ("CN", "UNKNOWN", "OTHER"):
        for kind in ("release", "price", "style", "colors"):
            for candidate in social[kind]:
                candidate["kind"] = "social_unverified"
        release_candidates.extend(social["release"])
        price_candidates.extend(social["price"])
        style_candidates.extend(social["style"])
        color_candidates.extend(social["colors"])

    # 国牌社媒没抽到字段时，退回本地档案的历史字段救急；口径仍标「历史原价」。
    if current.region == "CN" and archive_item is not None:
        if not social["release"] and archive_item.release_year:
            release_candidates.append({
                "value": archive_item.release_year,
                "kind": "archive",
                "raw": f"Released in {archive_item.release_year}",
                "channel": "lolibrary",
                "ref": archive_item.url,
            })
        if not social["price"] and archive_item.original_price:
            price_candidates.append({
                "value": archive_item.original_price,
                "kind": "archive_original",
                "raw": archive_item.original_price,
                "channel": "lolibrary",
                "ref": archive_item.url,
            })
        if not social["colors"]:
            for color in archive_item.colors:
                color_candidates.append({
                    "value": color,
                    "kind": "archive",
                    "raw": color,
                    "channel": "lolibrary",
                    "ref": archive_item.url,
                })

    def pick(candidates: list[dict], *, conflict: bool = True) -> tuple[str, str, bool]:
        values = _unique_values(candidates)
        if not values:
            return "", "", False
        if conflict and len(values) > 1:
            return "", candidates[0].get("kind", ""), True
        return values[0], candidates[0].get("kind", ""), False

    release, release_kind, release_conflict = pick(release_candidates)
    price_pool = [c for c in price_candidates if c.get("role") != "deposit"] or price_candidates
    price, price_kind, price_conflict = pick(price_pool)
    style, style_kind, style_conflict = pick([c for c in style_candidates if _value(c)])
    colors = _unique_values(color_candidates)[:8]
    style_conflict = style_conflict and bool(style)
    if style_conflict:
        style = ""

    missing = []
    if not release:
        missing.append("发售年份")
    if not price:
        missing.append("价格")
    if not style:
        missing.append("款式")
    if not colors:
        missing.append("配色")

    source_refs: list[str] = []
    if archive_item is not None and archive_item.url:
        source_refs.append(archive_item.url)
    for candidate in (*release_candidates, *price_candidates, *style_candidates, *color_candidates):
        ref = str(candidate.get("ref") or "")
        if ref and ref not in source_refs:
            source_refs.append(ref)

    profile = {
        "brand": current.brand,
        "brand_id": current.brand_id,
        "region": current.region,
        "region_label": _region_label(current.region),
        "title": current.series_en or (archive_item.title if archive_item else ""),
        "title_cn": current.series_cn,
        "product_number": current.product_number or (archive_item.product_number if archive_item else ""),
        "release": release,
        "release_kind": release_kind,
        "release_conflict": release_conflict,
        "price": price,
        "price_kind": price_kind,
        "price_conflict": price_conflict,
        "style": style,
        "style_conflict": style_conflict,
        "colors": colors,
        "url": archive_item.url if archive_item else "",
        "sources": source_refs[:5],
        "missing": missing,
        "confidence": current.confidence,
        "verified": current.verified,
        "source": current.source,
        "notes": notes,
    }

    evidence_item = {
        "channel": "dress",
        "source_kind": _profile_source_kind(current, archive_item, social),
        "ref": profile["url"] or profile["title"] or current.series_cn,
        "title": _profile_title(profile),
        "text": _clip(_profile_text(profile, archive_error)),
        "url": profile["url"],
    }
    return {"profile": profile, "evidence": evidence_item}


def _profile_source_kind(current: DressQuery, archive_item: ArchiveItem | None, social: dict) -> str:
    if not current.verified and current.source in ("model", "pending_model"):
        return "realtime"
    if any(social.get(kind) for kind in social):
        return "realtime"
    if current.verified and (
        current.source == "series_alias" or current.source.startswith("catalog_")
    ):
        return "third_party"
    if current.region == "JP" and archive_item is not None:
        return "third_party"
    if archive_item is not None:
        return "third_party"
    return "realtime"


def _profile_title(profile: dict) -> str:
    brand = profile.get("brand") or "品牌未识别"
    title = profile.get("title") or profile.get("title_cn") or "系列未识别"
    return _clip(f"裙子档案｜{brand}｜{title}")


def _profile_text(profile: dict, archive_error: str = "") -> str:
    parts: list[str] = []
    brand = profile.get("brand") or "品牌未识别"
    region = profile.get("region_label") or "地区未识别"
    series = profile.get("title") or profile.get("title_cn") or "系列未识别"
    if profile.get("title_cn") and profile.get("title") and profile["title_cn"] not in profile["title"]:
        series = f"{profile['title']}（{profile['title_cn']}）"
    parts.append(f"品牌：{brand}（{region}）")
    parts.append(f"系列：{series}")
    if profile.get("product_number"):
        parts.append(f"货号：{profile['product_number']}")

    release = profile.get("release") or "未查到"
    if profile.get("release_conflict"):
        release = "来源说法不一致"
    parts.append(f"发售：{release}")

    if profile.get("price"):
        label = "历史原价" if profile.get("price_kind") == "archive_original" else "价格（网友提到，未核实）"
        if profile.get("price_conflict"):
            label = "价格（来源说法不一致）"
        parts.append(f"{label}：{profile['price']}")
    else:
        parts.append("价格：未查到")

    style = profile.get("style") or "未查到"
    if profile.get("style_conflict"):
        style = "来源说法不一致"
    parts.append(f"款式：{style}")
    parts.append("配色：" + (" / ".join(profile.get("colors") or []) or "未查到"))

    notes = list(profile.get("notes") or [])
    if profile.get("price_kind") == "archive_original":
        notes.append("历史原价不代表现价")
    kinds = {profile.get("release_kind"), profile.get("price_kind"), profile.get("style_kind")}
    if "social_unverified" in kinds:
        notes.append("社媒字段未核实")
    if archive_error:
        notes.append(archive_error)
    if notes:
        parts.append("备注：" + "；".join(dict.fromkeys(notes)))
    if profile.get("url"):
        parts.append("来源：" + profile["url"])
    return "｜".join(parts)

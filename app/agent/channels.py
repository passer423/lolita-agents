"""多路召回通道：一源一通道（ADR-0001）。

每个来源是一个独立召回通道，统一签名 ``recall(channel, question, ctx) -> list[dict]``：

* 每条证据带 ``channel / source_kind / ref / title / text / url / score / retriever``；
  ``score`` / ``retriever`` 只用于排查（不同通道的量纲不可比，不参与跨通道排序）；
* 通道失败抛 :class:`ChannelUnavailable`，调用方（graph 的通道节点）跳过不猜；
* 实时通道（``xhs`` / ``weibo`` / ``web``）的结果只作本次回答的临时证据，
  **不落盘、不入索引**（小红书 6 小时缓存只是避免同一 query 反复启动浏览器）。

``source_kind`` 是**证据的效力等级**，不是来源分类——写作用它决定哪句能当规范：

* ``own_content``  自己整理的知识库，可作规范；
* ``third_party``  官方/档案馆数据，可作事实（但不含现价）；
* ``realtime``     网页搜索/小红书的结果摘要，时效信息可用，来源需标注；
* ``upstream_ugc`` 他人微博发言，**只能写成「有网友提到」，不得当规范**。

六通道：

* ``own``       自有知识库 ``content/kb``（``bridge.search_kb``）
* ``real``      上游真实语料索引（``bridge.search_real``）
* ``lolibrary`` Lolibrary 档案缓存 ``catalog.jsonl``（关键词匹配，身份/历史信息）
* ``xhs``       小红书（Playwright / Spider_XHS，可切换，可选）
* ``weibo``     微博实时（网页搜索限定微博域，可选）
* ``web``       网页搜索（博查 / 智谱，可选）

核心通道（``own`` / ``real`` / ``lolibrary``）每次提问并行召回；补查通道
（``xhs`` / ``weibo`` / ``web``）由 planner 判断是否追加。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app import bridge
from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

CORE_CHANNELS = ("own", "real", "lolibrary")
SUPPLEMENTARY_CHANNELS = ("xhs", "weibo", "web")
ALL_CHANNELS = (*CORE_CHANNELS, *SUPPLEMENTARY_CHANNELS)

# 通道 → 证据效力等级。``own`` 与 ``real`` 是同一召回机制（bridge + 向量检索），
# 但语料性质完全不同：前者自己写，后者是采集来的他人发言——混成一种就没有
# 「不许拿网友的话当规范」这条约束了。
CHANNEL_SOURCE_KIND = {
    "own": "own_content",
    "real": "upstream_ugc",
    "lolibrary": "third_party",
    "xhs": "realtime",
    "weibo": "realtime",
    "web": "realtime",
}

EVIDENCE_TEXT_LIMIT = 300
LOLIBRARY_TOP_K = 3
WEIBO_MAX_EVIDENCE = 3
# 微博页面在搜索结果里占比很低：只取 5 条再过滤，常常一条微博都剩不下。
# 先多拿候选再筛，比把搜索条数调大当默认值更精准（其他通道不需要这么多）。
WEIBO_CANDIDATES = 10


class ChannelUnavailable(RuntimeError):
    """通道不可用（未配置 / 未登录 / 依赖缺失）。调用方跳过，不猜数据。"""


@dataclass(slots=True)
class RecallContext:
    """一次召回所需的上下文（单用户：只有配置）。"""

    settings: AppSettings = field(default_factory=lambda: default_settings)


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EVIDENCE_TEXT_LIMIT else text[:EVIDENCE_TEXT_LIMIT] + "…"


# --------------------------------------------------------------------- 问题意图

# 操作型：用户要的是「在社群里怎么处理」——二手交易、求助、要不要下手。
# 真实帖子和社交平台里才有踩过坑的人；规范词条只能给定义。
#
# **刻意不收「怎么 / 如何 / 判断 / 注意什么」**：实测把这些算进来，7 条自有词条
# 的洗护/尺码/搭配题会被网友帖子挤下去（评测 MRR 0.882→0.800），代价远大于
# 收益。知识型「怎么洗」仍然是规范词条该答的题。
#
# 「收」「出」也不按单字匹配：它们混在「收藏」「出街」「出现」里的频率太高。
_OPERATIONAL_MARKERS = (
    "要不要", "该不该", "求助", "求问", "求推荐", "靠不靠谱", "踩雷", "避雷",
    "闲鱼", "二手", "收裙", "收一条", "收lo", "出物", "出二手", "出裙", "转手",
)

# 定义型：用户要的是「这是什么 / 两者有什么区别」。自有词条与品牌档案更可靠。
_DEFINITION_MARKERS = (
    "是什么", "什么意思", "区别", "定义", "含义", "指什么", "代表什么",
    "差在哪", "叫什么",
)

# 档案（lolibrary）只回答「这条裙子是什么 / 哪年出的 / 原价多少」。下面这些问法
# 要的是洗护、尺码、判断方法或名词解释——品牌档案一条都答不了，却会因为命中
# 品牌名占满前排（real-03 的三条 AP 档案）。只点名品牌时整通道跳过。
_ARCHIVE_IRRELEVANT_MARKERS = (
    "水洗标", "怎么洗", "能不能洗", "湿洗", "干洗", "洗护", "洗涤", "保养", "存放",
    "怎么选", "怎么判断", "注意什么", "怎么办", "尺码", "偏大", "偏小", "搭配",
    "是什么", "什么意思", "区别", "定义",
)

INTENT_OPERATIONAL = "operational"
INTENT_DEFINITION = "definition"


def question_intent(question: str) -> str:
    """问题意图（operational / definition / 空）。

    两者都命中时按 operational：二手/求助类问题即使夹着「是什么意思」，
    用户真正要的也是社群里的处理办法，名词解释只是附带。
    """
    text = question or ""
    if any(marker in text for marker in _OPERATIONAL_MARKERS):
        return INTENT_OPERATIONAL
    if any(marker in text for marker in _DEFINITION_MARKERS):
        return INTENT_DEFINITION
    return ""


# --------------------------------------------------------------------- own / real


def _kb_evidence(channel: str, hits: list[dict]) -> list[dict]:
    # score / retriever 原样带出去：上游 hits_to_dicts 已经算好了，丢掉它就只能
    # 靠重跑检索才能回答「这条为什么进来」——排查噪声证据时那是唯一的线索。
    return [
        {
            "channel": channel,
            "source_kind": CHANNEL_SOURCE_KIND[channel],
            "ref": hit.get("chunk_id") or "",
            "title": hit.get("author") or ("自有知识库" if channel == "own" else "过往网友讨论"),
            "text": _clip(hit.get("text") or ""),
            "url": hit.get("url") or "",
            "score": hit.get("score"),
            "retriever": hit.get("retriever") or "",
        }
        for hit in hits
    ]


# 「痛lo」这类中文紧贴拉丁字母的写法，上游分词会当成**一个整词**；而语料正文写的
# 是「痛 lo」，索引里存的是「痛」「lo」两个 token。整词在语料里 df=0，上游的
# 「问到语料之外」闸门于是把召回直接判空（评测 own-12 / own-12c 就是这么 miss 的）。
#
# 修在上游要连索引和全量评测一起回归，代价不对等；所以放在查询侧做**回退**：
# 只在首轮召回为空、且查询确实含中英边界时，给边界补一个空格重试一次。
# 刻意不做查询预处理：上游只吞「英文+空格+中文」的空格，不吞「中文+空格+英文」，
# 全局改写会改变普通问句的分词，而这类副作用借现有评测看不见。
# 上游分词修好后首轮就有结果，这个回退不会再触发，届时可以删掉。
_LATIN_CJK_BOUNDARY = re.compile(
    r"(?<=[A-Za-z])(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])(?=[A-Za-z])"
)


def _latin_cjk_variant(text: str) -> str:
    """在中英文边界补空格；没有边界（或补完没变化）时返回空串。"""
    text = (text or "").strip()
    variant = _LATIN_CJK_BOUNDARY.sub(" ", text)
    return variant if variant != text else ""


def _search_with_boundary_retry(
    search: Callable[..., list[dict]], text: str, ctx: RecallContext
) -> list[dict]:
    """先按原样检索；为空且含中英边界时，换「隔开写法」再试一次。

    「痛lo」为什么需要这一步见 ``_LATIN_CJK_BOUNDARY`` 上方。触发条件限定为
    **首轮为空**：有结果就完全不碰，普通问句的召回与排序不受影响。
    """
    hits = search(text, ctx.settings, top_k=ctx.settings.top_k)
    if hits:
        return hits
    variant = _latin_cjk_variant(text)
    if not variant:
        return hits
    logger.info("召回为空，按中英边界展开重试：%r -> %r", text, variant)
    return search(variant, ctx.settings, top_k=ctx.settings.top_k)


def _recall_own(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    hits = _search_with_boundary_retry(bridge.search_kb, query or question, ctx)
    return _kb_evidence("own", hits)


def _recall_real(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    hits = _search_with_boundary_retry(bridge.search_real, query or question, ctx)
    # 只保留「向量路也认可」的命中（retriever 为 vector / bm25+vector）。
    #
    # 只被 bm25 命中的片段 = 字面重合，不构成「语料里有答案」的证据：实测问
    # 「Baby 秋日私语 JSK 现在多少钱」时，real 通道 top3 全是纯 bm25，捞到的是
    # 「兔熊抵工资」这种只共享一个词的段子。分数切不开（bm25 归一化后同样是
    # 1.0），retriever 字段切得开。上游对「纯关键词命中」本来就要求 ≥2 个内容词
    # 才豁免向量下限（BM25_ONLY_MIN_CONTENT_MATCHES，为「谢安然穿哪个牌子」这类
    # 真答案留的口子）；这里更严一档：真实语料是他人发言，一旦进证据就可能被
    # writer 当成事实陈述，宁可漏也不能把道听途说当答案。
    semantic = [hit for hit in hits if "vector" in (hit.get("retriever") or "")]
    return _kb_evidence("real", semantic)


# --------------------------------------------------------------------- lolibrary

# 问句拆词：拉丁词取整词，中文去掉停用字后逐字作为匹配单元。
# Lolibrary 档案是英文/日文为主，中文问句能对上 brand/category/product_number 就够了。
_STOP_CHARS = set("的了是在和与或及吗呢啊吧都有什么怎么如何哪些哪个能否可以买想看看看")

_LATIN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _query_terms(question: str) -> list[str]:
    lowered = (question or "").lower()
    terms = _LATIN_RE.findall(lowered)
    for seg in _CJK_RE.findall(lowered):
        terms.extend(ch for ch in seg if ch not in _STOP_CHARS)
    return terms


# 品牌别名：圈内提问几乎只写缩写或中文叫法，档案里存的却是品牌全称。
# 值是档案 `brand` 字段的全称，分两类匹配：
# * 拉丁缩写按**整词**命中（`_LATIN_RE` 的 token），否则 "ap" 会命中 Cape / wrap；
# * 中文叫法按**子串**命中（中文没有词边界，且都 ≥2 字）。
# 「宝贝」「变态」这类故意不收：它们同时是日常用词，一旦当品牌，
# 「这个设计好变态」会被整句强行限定到 Metamorphose。
BRAND_ALIASES = {
    "ap": "Angelic Pretty",
    "baby": "Baby, the Stars Shine Bright",
    "btssb": "Baby, the Stars Shine Bright",
    "aatp": "Alice and the Pirates",
    "atp": "Alice and the Pirates",
    "etc": "Emily Temple Cute",
    "emily": "Emily Temple Cute",
    "meta": "Metamorphose Temps de Fille",
    "metamorphose": "Metamorphose Temps de Fille",
    "moitie": "Moi-même-Moitié",
    "mmm": "Moi-même-Moitié",
    "axes": "Axes Femme",
    "vw": "Vivienne Westwood",
    "miho": "Miho Matsuda",
    "hearte": "Heart E",
    "iw": "Innocent World",
    "vm": "Victorian Maiden",
    "jj": "Juliette et Justine",
    "jm": "Jane Marple",
    "kl": "Krad Lanrete",
    "mm": "Mary Magdalene",
    "安琪": "Angelic Pretty",
    "海盗": "Alice and the Pirates",
    "无辜世界": "Innocent World",
}


# 品类词不构成「相关」：任何一条 JSK 都能命中它。品牌查询可以靠它收窄，
# 但没点名品牌时，至少要命中一个品类词以外的词，否则术语题会带出无关档案。
_GENERIC_TERMS = {
    "jsk", "op", "skirt", "blouse", "cardigan", "coat", "jacket", "cape",
    "socks", "otks", "tights", "bag", "shoes", "accessory", "headdress",
    "set", "dress", "cutsew", "salopette", "pinafore",
}


def _brand_targets(question: str) -> tuple[set[str], str]:
    """找出问题里点名的品牌，返回 ``(品牌全称小写集合, 去掉中文别名后的文本)``。

    中文别名必须整词替换掉：否则「安琪」会被 `_query_terms` 拆成「安」「琪」
    两个单字继续参与打分，而别名本身永远拼不出来（这正是加中文别名前的老问题）。
    """
    lowered = (question or "").lower()
    targets = {BRAND_ALIASES[token] for token in _LATIN_RE.findall(lowered) if token in BRAND_ALIASES}

    cjk_aliases = sorted(
        (alias for alias in BRAND_ALIASES if _CJK_RE.search(alias)),
        key=len,
        reverse=True,
    )
    for alias in cjk_aliases:
        if alias in lowered:
            targets.add(BRAND_ALIASES[alias])
            lowered = lowered.replace(alias, " ")
    return {target.lower() for target in targets}, lowered


def _record_text(record: dict) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "brand", "category", "product_number")
    ) + " " + " ".join(record.get("colors") or []) + " " + " ".join(record.get("tags") or [])


def _term_in(term: str, text: str) -> bool:
    """拉丁词按词边界匹配：两三个字母的缩写真当子串用会误配（"op" 命中 "Top"）。"""
    if term.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _lolibrary_summary(record: dict) -> str:
    parts = [
        str(record.get("brand") or ""),
        str(record.get("category") or ""),
    ]
    if record.get("release_year"):
        parts.append(f"发售年份 {record['release_year']}")
    if record.get("original_price"):
        parts.append(f"历史原价 {record['original_price']}")
    if record.get("colors"):
        parts.append("配色 " + "/".join(record["colors"]))
    if record.get("notes"):
        parts.append(str(record["notes"]))
    return "｜".join(p for p in parts if p)


_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"path": "", "mtime": 0.0, "records": []}


def load_catalog(ctx: RecallContext) -> list[dict]:
    """读档案；进程内按 ``(路径, mtime)`` 缓存，档案更新后下一次请求自动换新。"""
    path = ctx.settings.lolibrary_catalog_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []

    key = str(path)
    with _catalog_lock:
        if _catalog_cache["path"] == key and _catalog_cache["mtime"] == mtime:
            return _catalog_cache["records"]

    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.info("Lolibrary 档案读取失败：%s", exc)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("slug"):
            records.append(record)

    with _catalog_lock:
        _catalog_cache.update(path=key, mtime=mtime, records=records)
    return records


def _recall_lolibrary(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    brand_targets, text = _brand_targets(query or question)
    # 别名已经展开成品牌全称，不能再当文本词打分（"ap" 会命中 "Cape"）。
    terms = [t for t in _query_terms(text) if t not in BRAND_ALIASES]
    if not terms and not brand_targets:
        return []
    # 没点名品牌时，还必须命中一个品类词以外的**拉丁**词：档案是英文的，中文单字
    # 本来就匹配不上（只会把「有什么区别」拆成「区」「别」「是」骗过判定）；
    # 而 "jsk" / "op" 这类品类词谁都命中，不足以说明相关。
    if not brand_targets and not any(
        term.isascii() and term not in _GENERIC_TERMS for term in terms
    ):
        return []

    # 洗护/尺码/名词解释类问题只点了品牌、没给系列：档案只有身份/年份/原价，
    # 答不了这些，却会凭品牌命中占满前排（real-03 的三条 AP 档案就是这么来的）。
    # 有系列名时不禁——那时档案是在回答「这条是什么」。
    if brand_targets and any(
        marker in (question or "") for marker in _ARCHIVE_IRRELEVANT_MARKERS
    ):
        series_terms = [
            term for term in terms if term.isascii() and term not in _GENERIC_TERMS
        ]
        if not series_terms:
            logger.info(
                "lolibrary 跳过：问题只点了品牌（%s），没有系列级检索词",
                "、".join(sorted(brand_targets)),
            )
            return []

    records = load_catalog(ctx)
    if not records:
        return []

    # 命中了品牌就只在品牌内挑：用户明确点名 "ap" / "安琪"，别家条目再相关也不该混进来。
    # 只点名品牌、没有其它词时（「安琪有什么经典款」），score=1 即可入选——
    # 此时排序退化为该品牌的档案顺序，正好可以当浏览用。
    # 没点名品牌时至少要命中两个词，且不能只靠品类词：只命中 "jsk" / "op" 的话，
    # 问「JSK 和 OP 有什么区别」会冒出几条无关条目——那是知识库该答的题，
    # 档案只该在用户点到具体品牌或系列时插话。
    min_score = 1 if brand_targets else 2

    scored: list[tuple[int, dict]] = []
    for record in records:
        text_content = _record_text(record).lower()
        brand_hit = any(target in str(record.get("brand") or "").lower() for target in brand_targets)
        if brand_targets and not brand_hit:
            continue
        score = sum(1 for term in terms if _term_in(term, text_content))
        if brand_hit:
            score += 1
        if score >= min_score:
            scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("slug") or "")))

    return [
        {
            "channel": "lolibrary",
            "source_kind": CHANNEL_SOURCE_KIND["lolibrary"],
            "ref": record.get("slug") or "",
            "title": str(record.get("title") or record.get("slug") or ""),
            "text": _clip(_lolibrary_summary(record)),
            "url": str(record.get("url") or ""),
        }
        for _score, record in scored[:LOLIBRARY_TOP_K]
    ]


# --------------------------------------------------------------------- xhs / weibo / web


def _search_evidence(channel: str, results: list) -> list[dict]:
    return [
        {
            "channel": channel,
            "source_kind": CHANNEL_SOURCE_KIND[channel],
            "ref": r.url,
            "title": r.title,
            "text": _clip(r.content or r.snippet),
            "url": r.url,
        }
        for r in results
    ]


def _recall_xhs(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import xhs_search

    results = xhs_search.search(
        query or question,
        ctx.settings,
        max_results=ctx.settings.xhs_max_results,
    )
    return _search_evidence("xhs", results)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# 微博域下这些子域不是「用户发言」：站内搜索页、登录/客服/服务页。
# 不排掉的话，site: 查询会把 service.weibo.com 当成命中，证据里全是空壳页。
_WEIBO_NON_CONTENT_HOSTS = {
    "s.weibo.com",
    "service.weibo.com",
    "passport.weibo.com",
    "login.weibo.com",
    "account.weibo.com",
    "help.weibo.com",
    "kefu.weibo.com",
    "app.weibo.com",
    "d.weibo.com",
}


def _is_weibo(url: str) -> bool:
    host = _host(url)
    if host in _WEIBO_NON_CONTENT_HOSTS:
        return False
    return (
        host == "weibo.com"
        or host.endswith(".weibo.com")
        or host == "weibo.cn"
        or host.endswith(".weibo.cn")
    )


def _weibo_queries(text: str) -> tuple[str, ...]:
    """两段式 query：先站内限定，再退回关键词诱导。

    `scripts/probe_search.py` 实测（智谱 search_std，3 条裙子查询）：
    ``site:weibo.com`` 平均命中 2.00 条，``{q} 微博`` / ``{q} 微博 实拍`` 只有 0.33。
    所以站内限定做主查询；关键词只在它一条都没带出时兜底。
    """
    keyword = text if "微博" in text else f"{text} 微博"
    return (f"site:weibo.com {text}", keyword)


def _weibo_relevant(result, text: str) -> bool:
    """判断这条微博是不是真在说这个问题。

    ``site:weibo.com`` 只保证域名，不保证话题：问「Baby 秋日私语 JSK」时，
    搜索引擎会带回一堆标题含「秋日私语」的秋天散文。规则：

    * 查询里有拉丁词（Baby / JSK / AP）：必须命中最少一个——品牌或款式是
      这条裙子的身份，只共享「秋日私语」这种泛化短语不算相关；
    * 纯中文查询（花与珍珠匣 缎带 测评）：命中**最长片段**（通常是品牌/系列名）。

    命中不足按 skip 报，不冒充「微博上没有」。
    """
    lowered = (text or "").lower()
    latin = _LATIN_RE.findall(lowered)
    cjk = _CJK_RE.findall(text or "")
    haystack = f"{result.title} {result.content or result.snippet}".lower()

    if latin:
        return any(_term_in(token, haystack) for token in latin)
    if cjk:
        return _term_in(max(cjk, key=len), haystack)
    return True


def _recall_weibo(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import web_search

    # 这是「经搜索引擎找微博页面」，不是微博站内搜索。query 构造直接决定命中率，
    # 先用 scripts/probe_search.py 实测再改，别凭感觉调。
    text = (query or question).strip()
    candidates = min(50, max(ctx.settings.search_max_results * 2, WEIBO_CANDIDATES))
    seen: set[str] = set()
    found: list[web_search.SearchResult] = []
    total = 0
    seen_weibo = 0
    fallback_error = ""

    for index, attempt in enumerate(_weibo_queries(text)):
        try:
            results = web_search.search(attempt, ctx.settings, max_results=candidates)
        except Exception as exc:  # noqa: BLE001
            if index == 0:
                # 连第一次都失败（缺 key / 网络）时交给 recall() 统一转 ChannelUnavailable，
                # 不要用第二次尝试把真实原因盖掉。
                raise
            fallback_error = f"{type(exc).__name__}: {str(exc)[:80]}"
            continue
        total += len(results)
        for result in results:
            if not _is_weibo(result.url) or result.url in seen:
                continue
            seen.add(result.url)
            seen_weibo += 1
            if _weibo_relevant(result, text):
                found.append(result)
        if len(found) >= WEIBO_MAX_EVIDENCE:
            break

    if not found:
        # 二阶检索（搜索 → 微博）取不到微博页，**不能**当成「微博上没有」：
        # 前者是检索策略没带出来，后者是事实缺失。两者混在一起，前端就会把
        # 「没查」渲染成「微博 → 空」，把策略失败伪装成事实缺失。
        # 这是 skip 而不是 empty —— 只有直连微博拿到空结果，才有资格说「没有」。
        if seen_weibo:
            detail = f"搜索引擎返回 {seen_weibo} 条微博页面，但均未命中查询词"
        elif total:
            detail = f"搜索引擎返回 {total} 条但无微博页面"
        else:
            detail = "搜索引擎 0 条结果"
        if fallback_error:
            detail += f"；关键词兜底失败（{fallback_error}）"
        raise ChannelUnavailable(f"微博通道未取到页面：{detail}")
    return _search_evidence("weibo", found[:WEIBO_MAX_EVIDENCE])


def _recall_web(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import web_search

    results = web_search.search(
        query or question, ctx.settings, max_results=ctx.settings.search_max_results
    )
    return _search_evidence("web", results)


# --------------------------------------------------------------------- 状态


_SEARCH_PROVIDERS = ("bocha", "zhipu")


def _search_status(app: AppSettings) -> dict[str, Any]:
    """网页搜索的就绪状态（weibo / web 共用同一套 key 与 provider）。"""
    if not app.search_enabled:
        return {
            "enabled": False,
            "ready": False,
            "reason": "未启用（SEARCH_ENABLED=false）",
        }
    if not app.search_api_key.strip():
        return {"enabled": True, "ready": False, "reason": "缺少 SEARCH_API_KEY"}
    provider = (app.search_provider or "bocha").strip().lower() or "bocha"
    if provider not in _SEARCH_PROVIDERS:
        return {
            "enabled": True,
            "ready": False,
            "reason": f"不支持的 SEARCH_PROVIDER：{provider}（只支持 bocha / zhipu）",
        }
    return {"enabled": True, "ready": True, "reason": ""}


def _xhs_status(app: AppSettings) -> dict[str, Any]:
    from app.services import xhs_search

    ready, reason = xhs_search.readiness(app)
    return {"enabled": app.xhs_enabled, "ready": ready, "reason": reason}


def channel_status(app: AppSettings | None = None) -> dict[str, dict[str, Any]]:
    """每个通道的 ``enabled / ready / reason``。

    ``enabled`` 是「配置里打开了」，``ready`` 是「现在真的能召回」——两者分开
    报，界面才能把「没开」和「开了但没登录」区分开，而不是笼统地显示不可用。
    核心通道（own / real / lolibrary）没有开关，恒为 enabled；没就绪时给的是
    可执行的原因（建索引 / 导档案）。
    """
    app = app or default_settings
    result: dict[str, dict[str, Any]] = {}

    for source in ("own", "real"):
        info = bridge.index_status(app, source=source)
        ready = bool(info.get("ready"))
        result[source] = {
            "enabled": True,
            "ready": ready,
            "reason": "" if ready else (
                info.get("error") or "索引还没建立：先运行 python scripts/build_kb_index.py"
            ),
        }

    records = load_catalog(RecallContext(settings=app))
    result["lolibrary"] = {
        "enabled": True,
        "ready": bool(records),
        "reason": "" if records else "档案为空：先运行 python scripts/import_lolibrary.py",
    }

    result["xhs"] = _xhs_status(app)
    search = _search_status(app)
    result["weibo"] = dict(search)
    result["web"] = dict(search)
    return result


def follow_source(app: AppSettings | None = None) -> str:
    """关注建议可用的实时来源：优先网页搜索，其次小红书；都不可用返回空。

    关注项本身就是「持续查某个关键词」；来源不可用时给按钮只会让用户点了
    也收不到更新，所以这里直接不给建议。
    """
    app = app or default_settings
    if _search_status(app)["ready"]:
        return "web"
    if _xhs_status(app)["ready"]:
        return "xhs"
    return ""


_HANDLERS = {
    "own": _recall_own,
    "real": _recall_real,
    "lolibrary": _recall_lolibrary,
    "xhs": _recall_xhs,
    "weibo": _recall_weibo,
    "web": _recall_web,
}


def recall(channel: str, question: str, ctx: RecallContext | None = None, *, query: str = "") -> list[dict]:
    """统一入口：按通道召回。通道失败抛 :class:`ChannelUnavailable`。

    **空列表是有效结果**（这一源里确实没有），不是错误；只有通道本身不可用
    （未配置 / 未登录 / 依赖缺失 / 意外异常）才抛异常，由调用方跳过。
    """
    ctx = ctx or RecallContext()
    question = (question or "").strip()
    if not question:
        return []

    handler = _HANDLERS.get(channel)
    if handler is None:
        raise ChannelUnavailable(f"未知通道：{channel}")
    try:
        items = handler(question, ctx, query=query)
    except ChannelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单通道意外异常局部化，不拖垮整个召回
        logger.warning("通道 %s 召回失败：%s: %s", channel, type(exc).__name__, exc)
        # 带上原始原因：只留异常类名时，界面只能显示「召回失败：SearchUnavailable」，
        # 用户看不出是缺 key 还是网络问题。截断避免把整段堆栈塞进 steps。
        raise ChannelUnavailable(
            f"{channel} 召回失败：{type(exc).__name__}: {str(exc)[:160]}"
        ) from exc

    # 意图是**问题级**属性，但合并层（``graph._normalize``）只拿到证据列表，
    # 拿不到问题。打在每条证据上，评测复用同一个合并函数时权重才不会丢。
    intent = question_intent(question)
    if intent and items:
        return [{**item, "intent": intent} for item in items]
    return items

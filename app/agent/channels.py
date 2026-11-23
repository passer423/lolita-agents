"""多路召回通道：一源一通道（ADR-0001）。

每个来源是一个独立召回通道，统一签名 ``recall(channel, question, ctx) -> list[dict]``：

* 每条证据带 ``channel / source_kind / ref / title / text / url / score / retriever``；
  ``score`` / ``retriever`` 只用于排查（不同通道的量纲不可比，不参与跨通道排序）；
* 通道失败抛 :class:`ChannelUnavailable`，调用方（graph 的通道节点）跳过不猜；
* 实时通道（``xhs`` / ``weibo`` / ``web``）的结果只作本次回答的临时证据，
  **不落盘、不入索引**（小红书 6 小时缓存只是避免同一 query 反复启动浏览器）。

``source_kind`` 是**证据的效力等级**，不是来源分类——写作用它决定哪句能当规范：

* ``own_content``  自己整理的知识库，可作规范；
* ``third_party``  官方/档案馆数据，可作事实（价格按发售价记录）；
* ``realtime``     网页搜索/小红书的结果摘要，时效信息可用，来源需标注；
* ``upstream_ugc`` 他人微博发言，**只能写成「有网友提到」，不得当规范**；
* ``image_text``   原帖配图的机器识别文字，**未校验**，只能写成「图上标着…」。

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
from app.agent import terms
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

# real 通道：纯 bm25 命中排第一且领先第二名多少分时才放行（见 _recall_real）。
_REAL_BM25_TOP_MARGIN = 0.25

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


def _clip(text: str, limit: int | None = None) -> str:
    text = (text or "").strip()
    cap = EVIDENCE_TEXT_LIMIT if limit is None else max(0, int(limit))
    if cap <= 0:
        return text
    return text if len(text) <= cap else text[:cap] + "…"


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


# 洗护/安全类：答案常埋在长帖后半段，且答错会伤衣服——两种情况都需要更完整的
# 正文，而不是 300 字的开头。
CARE_MARKERS = (
    "怎么洗", "能不能洗", "能洗吗", "水洗标", "水洗", "湿洗", "干洗", "洗涤",
    "洗护", "掉色", "浮色", "染色", "串色", "缩水", "褪色", "保养", "存放",
    "熨烫", "起球", "发黄",
)

# 定义/对比/搭配题的结论通常在后半段（own-08 的「同系列的 OP…」）。放宽这两类，
# 其余问题保持默认窗口，避免整体 prompt 变贵。
_CONFIG_WORDS = ("区别", "搭配", "怎么配", "怎么选")


def evidence_text_limit(question: str, settings: AppSettings) -> int:
    """按问题类型选单条证据的正文窗口。"""
    text = question or ""
    long_window = (
        question_intent(text) == INTENT_DEFINITION
        or any(marker in text for marker in CARE_MARKERS)
        or any(marker in text for marker in _CONFIG_WORDS)
    )
    if long_window:
        return int(settings.evidence_text_limit_long)
    return int(settings.evidence_text_limit)


# --------------------------------------------------------------------- own / real


def _kb_evidence(channel: str, hits: list[dict], limit: int | None = None) -> list[dict]:
    # score / retriever 原样带出去：上游 hits_to_dicts 已经算好了，丢掉它就只能
    # 靠重跑检索才能回答「这条为什么进来」——排查噪声证据时那是唯一的线索。
    return [
        {
            "channel": channel,
            "source_kind": CHANNEL_SOURCE_KIND[channel],
            "ref": hit.get("chunk_id") or "",
            "title": hit.get("author") or ("自有知识库" if channel == "own" else "过往网友讨论"),
            "text": _clip(hit.get("text") or "", limit),
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


def _dedupe_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in queries:
        query = (raw or "").strip()
        if query and query not in seen:
            seen.add(query)
            ordered.append(query)
    return ordered


def _own_queries(text: str, ctx: RecallContext, *, definition: bool) -> list[str]:
    """own 通道的检索顺序：定义题先走术语规范写法，再退回原写法/边界/别名。

    为什么规范写法排在最前：KB 里同一个术语可能有多种写法（``痛lo`` / ``痛 lo``），
    而上游分词把「痛lo」当一个整词、把「痛 lo」切成两个词，BM25 结果会随写法
    漂移。词典把查询统一成词条使用的写法，等于把「召回碰运气」变成确定性行为。
    非定义题不做这一步：普通洗护/搭配题的查询不该被词典改写。
    """
    queries: list[str] = []
    if definition:
        canonical = terms.canonical_query(text, ctx.settings)
        if canonical:
            queries.append(canonical)
    queries.append(text)
    variant = _latin_cjk_variant(text)
    if variant:
        queries.append(variant)
    if definition:
        queries.extend(terms.alias_queries(text, ctx.settings))
    return _dedupe_queries(queries)


def _search_own(text: str, ctx: RecallContext, *, gate_text: str = "") -> list[dict]:
    """own 检索 + 定义题术语门禁。

    门禁放在**每个候选查询之后**：如果规范写法召回的是别的术语词条，先被门禁
    丢掉，再继续尝试下一个写法；这样「问 A 命中 B 词条」不会被当成结果返回。
    """
    gate_text = gate_text or text
    definition = question_intent(gate_text) == INTENT_DEFINITION
    if definition and not terms.matched_terms(text, ctx.settings):
        # 规划器可能把问题压短到不含术语；这时以原问题为准做规范改写与门禁。
        text = gate_text
    for query in _own_queries(text, ctx, definition=definition):
        hits = bridge.search_kb(query, ctx.settings, top_k=ctx.settings.top_k)
        if not hits:
            continue
        if definition:
            hits = terms.filter_own_hits(gate_text, hits, ctx.settings)
        if hits:
            return hits
    return []


def _recall_own(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
    hits = _search_own(query or question, ctx, gate_text=question or query)
    return _kb_evidence("own", hits, limit)


def _recall_real(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
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
    #
    # 例外：纯 bm25 命中排在整条结果第一位、且与第二名有明显分差时放行这一条。
    # 这类命中是「整句字面强命中 + 其余路都不认」，与上面那种只共享一个词的
    # 噪声不同；分差要求保证它没有和别的候选贴在一起。阈值 0.25 的依据：分数
    # 已按各自召回池归一化到 [0,1]，0.25 意味着第二名只剩 ≤75% 的置信度，
    # 是明确的第一名而不是近乎并列；实测全量 52 条里，纯 bm25 首名只出现在
    # 5 条 real 金标上，其中 3 条没有第二名（分差 1.0），2 条第二名同为 1.0
    # （多意图拆句后各自归一化），阈值落在这段空档里，不会误伤中间态。
    semantic = [hit for hit in hits if "vector" in (hit.get("retriever") or "")]
    top = hits[0] if hits else None
    if (
        top is not None
        and "bm25" in (top.get("retriever") or "")
        and "vector" not in (top.get("retriever") or "")
        and (len(hits) == 1 or top.get("score", 0.0) - hits[1].get("score", 0.0) >= _REAL_BM25_TOP_MARGIN)
    ):
        semantic = [top, *semantic]
    return _kb_evidence("real", semantic, limit)


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
        parts.append(f"发售价 {record['original_price']}")
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


def _recall_lolibrary(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
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
            "text": _clip(_lolibrary_summary(record), limit),
            "url": str(record.get("url") or ""),
        }
        for _score, record in scored[:LOLIBRARY_TOP_K]
    ]


# --------------------------------------------------------------------- xhs / weibo / web


def _evidence_ref(result) -> str:
    """证据去重键：正常用 URL；无链接的文本兜底用来源+标题，避免全被合并成一条。"""
    url = str(getattr(result, "url", "") or "").strip()
    if url:
        return url
    if getattr(result, "text_only", False):
        provider = str(getattr(result, "provider", "") or "")
        title = str(getattr(result, "title", "") or "")[:60]
        return f"text:{provider}:{title}"
    return ""


def _search_evidence(channel: str, results: list, limit: int | None = None) -> list[dict]:
    return [
        {
            "channel": channel,
            "source_kind": CHANNEL_SOURCE_KIND[channel],
            "ref": _evidence_ref(r),
            "title": r.title,
            "text": _clip(r.content or r.snippet, limit),
            "url": r.url,
            # 1 = 只有标题/摘要、没有可点击原链接（360 文本兜底）。写作层与
            # 引用层看到它为真时，不得生成可点击来源。
            "text_only": bool(getattr(r, "text_only", False)),
            # 只给数量与前几张图：正文里没有的信息，靠它提示「图里可能有价格表」。
            # 这两个字段不进写作提示词，只有代码侧读它们。
            "image_count": int(getattr(r, "image_count", 0) or 0),
            "image_urls": list(getattr(r, "image_urls", None) or []),
            # 作者只用于判断「是不是品牌官方账号发的」，不进提示词。
            "author_id": str(getattr(r, "author_id", "") or ""),
            "author_name": str(getattr(r, "author_name", "") or ""),
        }
        for r in results
    ]


# 会去读官方账号主页的问题类型：定价与开团/再贩时间。洗护、尺码、搭配不在此列，
# 那些问题看官方上新帖也用不上，不该为它多等。
_OFFICIAL_INTENT_MARKERS = (
    "价格", "多少钱", "定价", "定金", "尾款", "预约", "现货", "再贩", "开团",
    "上新", "发售", "开售", "排期", "截止", "活动价", "优惠", "什么时候",
)


def _mark_official(items: list[dict], account: dict) -> bool:
    """把官方账号发的证据标出来；返回是否有命中。

    比对用 ``author_id``：约 10% 的笔记没有作者字段，这些会漏判——已知且接受，
    不为了补作者再打一次接口。
    """
    user_id = str((account or {}).get("user_id") or "")
    if not user_id:
        return False
    hit = False
    for item in items:
        if str(item.get("author_id") or "") == user_id:
            item["official"] = True
            hit = True
    return hit


def _official_evidence(
    question: str, ctx: RecallContext, items: list[dict], limit: int | None
) -> list[dict]:
    """品牌官方账号的补充证据：现有召回里没有官方帖时，才去账号主页拿。

    顺序固定：品牌 → 官方账号（白名单/缓存/搜一次）→ 已有结果里标记 → 还不够
    才读账号主页一页。任何一步失败都返回空表，调用方照旧用现有 UGC 召回。
    """
    from app.services import xhs_accounts

    brand, series = xhs_accounts.brand_for_question(question, ctx.settings)
    if brand is None:
        return []
    account = xhs_accounts.resolve(brand, ctx.settings)
    if not account:
        return []
    # 标记是免费的（只是比对已有结果的 author_id），所以先做。
    if _mark_official(items, account):
        return []
    # 读主页要花请求，只为「定价/开团/再贩时间」这类问题花——洗护、尺码、搭配
    # 问了也用不上官方上新帖，不该让它们多等一两秒。
    if not any(marker in question for marker in _OFFICIAL_INTENT_MARKERS):
        return []
    notes = xhs_accounts.official_notes(account, ctx.settings, series=series)
    if not notes:
        return []
    from app.services import xhs_search

    extra = _search_evidence("xhs", xhs_search._notes_to_results(notes), limit)
    nickname = str(account.get("nickname") or "")
    for item in extra:
        item["official"] = True
        item["official_account"] = nickname
    return extra


def _recall_xhs(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
    # 总开关关闭时直接跳过：不发任何请求，也不进入登录墙/图片识别链路。
    # 状态接口会显示 enabled=false + 原因，界面不会把它当成「有通道但永远为空」。
    if not ctx.settings.xhs_enabled:
        raise ChannelUnavailable("小红书通道已关闭（XHS_ENABLED=false），不发起请求")

    from app.agent import image_evidence
    from app.services import xhs_search

    results = xhs_search.search(
        query or question,
        ctx.settings,
        max_results=ctx.settings.xhs_max_results,
    )
    items = _search_evidence("xhs", results, limit)
    # 官方账号是增强：查不到就什么也不加，现有召回与排序不受影响。
    try:
        items.extend(_official_evidence(question, ctx, items, limit))
    except Exception as exc:  # noqa: BLE001 - 账号链路失败不能拖垮召回
        logger.warning("官方账号链路失败，跳过：%s: %s", type(exc).__name__, exc)
    # 图里的字是**另一条证据**：不拼进原帖正文。识别失败只缺这一条，不影响召回。
    try:
        items.extend(image_evidence.collect(question, items, ctx))
    except Exception as exc:  # noqa: BLE001 - 图片识别属于增强，不能拖垮整路召回
        logger.warning("图片文字识别失败，跳过：%s: %s", type(exc).__name__, exc)
    return items


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


def _with_image_text(question: str, items: list[dict], ctx: RecallContext) -> list[dict]:
    """配图里的字是另一条证据；识别失败只缺这一条，不影响召回。"""
    from app.agent import image_evidence

    try:
        items.extend(image_evidence.collect(question, items, ctx))
    except Exception as exc:  # noqa: BLE001 - 图片识别属于增强，不能拖垮整路召回
        logger.warning("图片文字识别失败，跳过：%s: %s", type(exc).__name__, exc)
    return items


def _weibo_direct_fallback(
    question: str, text: str, ctx: RecallContext, limit: int | None
) -> list[dict]:
    """搜索引擎配额耗尽时的兜底：直抓微博站内第一页。

    直抓失败（登录墙 / 超时 / 没装 Playwright）按通道 skip 上报，不冒充
    「微博上没有」，也不拖累其他通道。
    """
    from app.services import weibo_direct

    try:
        results = weibo_direct.search(
            text, ctx.settings, max_results=ctx.settings.weibo_direct_max_results
        )
    except weibo_direct.WeiboDirectUnavailable as exc:
        raise ChannelUnavailable(f"微博直抓不可用：{exc}") from exc
    if not results:
        raise ChannelUnavailable("微博直抓第一页没有与查询词相关的帖子")
    items = _search_evidence("weibo", results[:WEIBO_MAX_EVIDENCE], limit)
    # 直抓能拿到配图，价格往往就在图里：让 OCR 有机会补一条 image_text 证据。
    return _with_image_text(question, items, ctx)


def _recall_weibo(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
    from app.services import web_search

    # 这是「经搜索引擎找微博页面」，不是微博站内搜索。query 构造直接决定命中率，
    # 先用 scripts/probe_search.py 实测再改，别凭感觉调。
    text = (query or question).strip()

    # 公网演示不配付费 key：搜索不可用时直抓不是「应急」，而是这条通道的主路。
    configured, config_reason = web_search.paid_config_status(ctx.settings)
    if not configured:
        logger.info("付费搜索未配置（%s），微博直抓作为独立来源", config_reason)
        return _weibo_direct_fallback(question, text, ctx, limit)

    # 配额已失效时不必再打一次注定失败的接口，直接换直抓。
    if web_search.quota_blocked(ctx.settings):
        logger.warning("微博搜索配额失效中，改用直抓")
        return _weibo_direct_fallback(question, text, ctx, limit)

    candidates = min(50, max(ctx.settings.search_max_results * 2, WEIBO_CANDIDATES))
    seen: set[str] = set()
    found: list[web_search.SearchResult] = []
    total = 0
    seen_weibo = 0
    fallback_error = ""

    for index, attempt in enumerate(_weibo_queries(text)):
        try:
            results = web_search.search(attempt, ctx.settings, max_results=candidates)
        except web_search.SearchQuotaExhausted as exc:
            # 1113 是账号级资源包耗尽：不重试、不做第二次查询，直接换直抓。
            logger.warning("微博搜索配额耗尽（1113），改用直抓：%s", exc)
            return _weibo_direct_fallback(question, text, ctx, limit)
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
    items = _search_evidence("weibo", found[:WEIBO_MAX_EVIDENCE], limit)
    return _with_image_text(question, items, ctx)


def _recall_web(
    question: str, ctx: RecallContext, query: str = "", limit: int | None = None
) -> list[dict]:
    from app.services import web_search

    text = query or question
    configured, _ = web_search.paid_config_status(ctx.settings)
    if not configured or web_search.quota_blocked(ctx.settings):
        # 无 key / 配额失效：Bing 浏览器主路，失败或为空才用 360 文本兜底。
        results = web_search.search_no_key(
            text, ctx.settings, max_results=ctx.settings.search_max_results
        )
    else:
        try:
            results = web_search.search(
                text, ctx.settings, max_results=ctx.settings.search_max_results
            )
        except web_search.SearchQuotaExhausted:
            logger.warning("网页搜索配额耗尽，改用 Bing 浏览器检索")
            results = web_search.search_no_key(
                text, ctx.settings, max_results=ctx.settings.search_max_results
            )
    return _search_evidence("web", results, limit)


# --------------------------------------------------------------------- 状态


_SEARCH_PROVIDERS = ("bocha", "zhipu")


def _search_status(app: AppSettings) -> dict[str, Any]:
    """网页搜索的就绪状态（weibo / web 共用同一套 key 与 provider）。"""
    from app.services import web_search

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
    if web_search.quota_blocked(app, provider):
        # 不能只按「key 还在」报 ready：账号资源包耗尽时接口 0.17s 就失败，
        # 界面还显示就绪，用户会以为微博通道正常。
        return {
            "enabled": True,
            "ready": False,
            "reason": "搜索配额失效（code=1113 余额/资源包耗尽），暂按不可用降级",
        }
    return {"enabled": True, "ready": True, "reason": ""}


def _weibo_status(app: AppSettings) -> dict[str, Any]:
    """微博通道状态：如实标明当前走搜索还是直抓。

    * 付费搜索配好且可用：``mode=search``；
    * 没配 key / provider 不支持：直抓就是主路，``mode=direct``；
    * 配额失效：搜索不可用，直抓可用则 ``mode=direct``；
    * 直抓开关也关掉：``mode=unavailable``；直抓是当前路由但浏览器缺失时
      仍是 ``mode=direct`` + ``ready=false``，并给出具体原因。
    """
    from app.services import web_search, weibo_direct

    search = _search_status(app)
    configured, config_reason = web_search.paid_config_status(app)
    if not configured:
        ready, reason = weibo_direct.readiness(app)
        if ready:
            return {
                "enabled": True,
                "ready": True,
                "mode": "direct",
                "reason": f"付费搜索未配置（{config_reason}），使用微博直抓（站内第一页）",
            }
        if not app.weibo_direct_enabled:
            return {
                "enabled": False,
                "ready": False,
                "mode": "unavailable",
                "reason": f"付费搜索未配置（{config_reason}）；微博直抓已关闭（WEIBO_DIRECT_ENABLED=false）",
            }
        return {
            "enabled": True,
            "ready": False,
            "mode": "direct",
            "reason": f"付费搜索未配置（{config_reason}）；微博直抓不可用（{reason}）",
        }

    if search["ready"]:
        return {**search, "mode": "search"}

    if web_search.quota_blocked(app):
        ready, reason = weibo_direct.readiness(app)
        if ready:
            return {
                "enabled": True,
                "ready": True,
                "mode": "direct",
                "reason": "搜索引擎配额失效，改用微博直抓（站内第一页）",
            }
        return {
            **search,
            "mode": "direct",
            "reason": f"{search['reason']}；微博直抓不可用（{reason}）",
        }

    return {**search, "mode": "search"}


def _web_status(app: AppSettings) -> dict[str, Any]:
    """网页通道状态：付费搜索可用走 search，否则无 key 的 Bing/360 走 browser。"""
    from app.services import web_search

    search = _search_status(app)
    if search["ready"]:
        return {**search, "mode": "search"}

    configured, config_reason = web_search.paid_config_status(app)
    if not app.web_browser_enabled:
        # 没有付费搜索、也没有开无 key 来源：这条通道没有可用路径。
        status = {**search, "mode": "unavailable"}
        if not configured:
            status["reason"] = f"{config_reason}；无 key 网页来源未启用（WEB_BROWSER_ENABLED=false）"
        return status

    ready, reason = web_search.no_key_readiness(app)
    if ready:
        detail = "搜索引擎配额失效" if configured else f"付费搜索未配置（{config_reason}）"
        return {
            "enabled": True,
            "ready": True,
            "mode": "browser",
            "reason": f"{detail}，改用 Bing 浏览器检索（失败时 360 文本兜底）",
        }
    detail = search["reason"] if configured else config_reason
    return {
        "enabled": True,
        "ready": False,
        # 开关是开的，路由就是 browser；ready=false + 原因说明当前为什么不可用。
        "mode": "browser",
        "reason": f"{detail}；无 key 网页来源不可用（{reason}）",
    }


def _xhs_status(app: AppSettings) -> dict[str, Any]:
    from app.services import xhs_search

    # readiness() 只看结构；status_readiness() 额外带上「最近真实调用失败 /
    # 连续空结果」，且不会反过来拦召回（见 xhs_search.status_readiness）。
    ready, reason = xhs_search.status_readiness(app)
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
    result["weibo"] = _weibo_status(app)
    result["web"] = _web_status(app)
    return result


def follow_source(app: AppSettings | None = None) -> str:
    """关注建议可用的实时来源：优先网页搜索，其次小红书；都不可用返回空。

    关注项本身就是「持续查某个关键词」；来源不可用时给按钮只会让用户点了
    也收不到更新，所以这里直接不给建议。
    """
    app = app or default_settings
    if _web_status(app)["ready"]:
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
    limit = evidence_text_limit(question, ctx.settings)
    try:
        items = handler(question, ctx, query=query, limit=limit)
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

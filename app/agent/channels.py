"""多路召回通道：一源一通道（ADR-0001）。

每个来源是一个独立召回通道，统一签名 ``recall(channel, question, ctx) -> list[dict]``：

* 每条证据带 ``channel / ref / title / text / url``；
* 通道失败抛 :class:`ChannelUnavailable`，调用方（graph 的通道节点）跳过不猜；
* 实时通道（``xhs`` / ``weibo`` / ``web``）的结果只作本次回答的临时证据，
  **不落盘、不入索引**（小红书 6 小时缓存只是避免同一 query 反复启动浏览器）。

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
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app import bridge
from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

CORE_CHANNELS = ("own", "real", "lolibrary")
SUPPLEMENTARY_CHANNELS = ("xhs", "weibo", "web")
ALL_CHANNELS = (*CORE_CHANNELS, *SUPPLEMENTARY_CHANNELS)

EVIDENCE_TEXT_LIMIT = 300
LOLIBRARY_TOP_K = 3


class ChannelUnavailable(RuntimeError):
    """通道不可用（未配置 / 未登录 / 依赖缺失）。调用方跳过，不猜数据。"""


@dataclass(slots=True)
class RecallContext:
    """一次召回所需的上下文。偏好用于个性化排序；v1 只做单用户记忆。"""

    settings: AppSettings = field(default_factory=lambda: default_settings)
    preferences: dict[str, list[str]] = field(default_factory=dict)


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EVIDENCE_TEXT_LIMIT else text[:EVIDENCE_TEXT_LIMIT] + "…"


# --------------------------------------------------------------------- own / real


def _kb_evidence(channel: str, hits: list[dict]) -> list[dict]:
    return [
        {
            "channel": channel,
            "ref": hit.get("chunk_id") or "",
            "title": hit.get("author") or ("自有知识库" if channel == "own" else "真实语料"),
            "text": _clip(hit.get("text") or ""),
            "url": "",
        }
        for hit in hits
    ]


def _recall_own(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    hits = bridge.search_kb(query or question, ctx.settings, top_k=ctx.settings.top_k)
    return _kb_evidence("own", hits)


def _recall_real(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    hits = bridge.search_real(query or question, ctx.settings, top_k=ctx.settings.top_k)
    return _kb_evidence("real", hits)


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


def _record_text(record: dict) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("title", "brand", "category", "product_number")
    ) + " " + " ".join(record.get("colors") or []) + " " + " ".join(record.get("tags") or [])


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


def _load_catalog(ctx: RecallContext) -> list[dict]:
    path = ctx.settings.lolibrary_catalog_path
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("slug"):
                records.append(record)
    except OSError as exc:
        logger.info("Lolibrary 档案读取失败：%s", exc)
        return []
    return records


def _recall_lolibrary(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    terms = _query_terms(query or question)
    if not terms:
        return []
    records = _load_catalog(ctx)
    if not records:
        return []

    scored: list[tuple[int, dict]] = []
    for record in records:
        text = _record_text(record).lower()
        score = sum(1 for term in terms if term in text)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("slug") or "")))

    return [
        {
            "channel": "lolibrary",
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
            "ref": r.url,
            "title": r.title,
            "text": _clip(r.content or r.snippet),
            "url": r.url,
        }
        for r in results
    ]


def _recall_xhs(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import xhs_search

    results = xhs_search.search(query or question, ctx.settings, max_results=3)
    return _search_evidence("xhs", results)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_weibo(url: str) -> bool:
    host = _host(url)
    return host == "weibo.com" or host.endswith(".weibo.com") or host == "weibo.cn" or host.endswith(".weibo.cn")


def _recall_weibo(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import web_search

    text = " ".join(part for part in ((query or question), "微博") if part).strip()
    results = web_search.search(text, ctx.settings, max_results=3)
    return _search_evidence("weibo", [r for r in results if _is_weibo(r.url)])


def _recall_web(question: str, ctx: RecallContext, query: str = "") -> list[dict]:
    from app.services import web_search

    results = web_search.search(
        query or question, ctx.settings, max_results=ctx.settings.search_max_results
    )
    return _search_evidence("web", results)


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
        return handler(question, ctx, query=query)
    except ChannelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单通道意外异常局部化，不拖垮整个召回
        logger.warning("通道 %s 召回失败：%s: %s", channel, type(exc).__name__, exc)
        raise ChannelUnavailable(f"{channel} 召回失败：{type(exc).__name__}") from exc

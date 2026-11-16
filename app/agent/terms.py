"""Lolita 术语词典：查询侧归一、别名扩展与定义题门禁。

词典只解决两件事：

* **同一个术语有几种写法**（``痛lo`` / ``痛 lo`` / ``痛LO``，``JSK`` / ``jumper skirt``）；
* **这条问题问的是哪个词条**，从而在定义题里只接受该词条自己的证据。

它**不承载规范答案**：``definition_oneline`` / ``common_misuse`` 只给人看，不注入
prompt；答案仍由 ``content/kb`` 的词条支撑。词典文件缺失或损坏时退回空词典，
行为与加词典前完全一致（不抛错、不影响任何普通问句）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import AppSettings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Term:
    id: str
    term: str
    match: tuple[str, ...]
    aliases: tuple[str, ...]
    kb_ref: str
    related: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TermMatch:
    term: Term
    start: int
    end: int


_cache: dict[str, object] = {"path": "", "mtime": 0.0, "terms": ()}
_lock = threading.Lock()

_LATIN_CJK_SPACE = re.compile(
    r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9])"
)


def terms_path(app: AppSettings | None = None) -> Path:
    """词典路径。放在 ``content/terms`` 下，跟知识库一起进 git。"""
    return Path((app or default_settings).content_dir) / "terms" / "lolita_terms.json"


def load_terms(app: AppSettings | None = None) -> tuple[Term, ...]:
    """读词典；进程内按 ``(路径, mtime)`` 缓存，改文件后下一次请求自动换新。"""
    path = terms_path(app)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()

    with _lock:
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return _cache["terms"]  # type: ignore[return-value]

    terms = _read(path)
    with _lock:
        _cache.update(path=str(path), mtime=mtime, terms=terms)
    return terms


def _read(path: Path) -> tuple[Term, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("术语词典读取失败（不影响回答）：%s", exc)
        return ()
    except json.JSONDecodeError as exc:
        logger.warning("术语词典不是合法 JSON（不影响回答）：%s", exc)
        return ()

    raw = payload.get("terms") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        logger.warning("术语词典结构不对：期望 terms 列表")
        return ()

    terms: list[Term] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        match = _clean_list(item.get("match"))
        if not term or not match:
            logger.warning("术语词典跳过一条缺 term/match 的条目：%r", item)
            continue
        if term in match:
            match.remove(term)
        term_id = str(item.get("id") or term).strip()
        if term_id in seen:
            logger.warning("术语词典 id 重复，跳过：%s", term_id)
            continue
        seen.add(term_id)
        terms.append(
            Term(
                id=term_id,
                term=term,
                match=(term, *match),
                aliases=tuple(_clean_list(item.get("aliases"))),
                kb_ref=str(item.get("kb_ref") or "").strip(),
                related=tuple(_clean_list(item.get("related"))),
            )
        )
    logger.info("术语词典已装载：%s 条（%s）", len(terms), path)
    return tuple(terms)


def _clean_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _squash(text: str) -> str:
    """去掉中英之间的空格，只用于「正文里有没有提到这个术语」的包含判断。"""
    return _LATIN_CJK_SPACE.sub("", _normalize(text))


def _is_latin(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _surface_pattern(surface: str) -> str:
    """把术语写法转成正则：中英之间允许空格，拉丁词要求整词边界。

    「痛lo」要能匹配「痛 lo」；「OP」不能匹配「open」；「SK」不能匹配「skirt」。
    """
    surface = _normalize(surface)
    parts: list[str] = []
    previous = ""
    for char in surface:
        if char.isspace():
            if parts and parts[-1] != r"\s*":
                parts.append(r"\s*")
            continue
        if (
            previous
            and _is_latin(char) != _is_latin(previous)
            and (_is_cjk(char) or _is_cjk(previous))
            and (not parts or parts[-1] != r"\s*")
        ):
            parts.append(r"\s*")
        parts.append(re.escape(char))
        previous = char
    pattern = "".join(parts)
    if surface[:1].isascii() and surface[:1].isalnum():
        pattern = r"(?<![a-z0-9])" + pattern
    if surface[-1:].isascii() and surface[-1:].isalnum():
        pattern = pattern + r"(?![a-z0-9])"
    return pattern


def match_terms(text: str, app: AppSettings | None = None) -> list[TermMatch]:
    """找出文本里出现的术语；重叠时保留最长匹配，结果按出现位置排序。"""
    normalized = _normalize(text)
    candidates: list[TermMatch] = []
    for term in load_terms(app):
        for surface in term.match:
            for match in re.finditer(_surface_pattern(surface), normalized):
                candidates.append(TermMatch(term, match.start(), match.end()))

    # 先长后短、再按位置；重叠的只保留最长那个（「痛包」不能被「痛」抢走）。
    candidates.sort(key=lambda item: (-(item.end - item.start), item.start))
    chosen: list[TermMatch] = []
    for candidate in candidates:
        if all(
            candidate.end <= other.start or candidate.start >= other.end
            for other in chosen
        ):
            chosen.append(candidate)
    chosen.sort(key=lambda item: item.start)
    return chosen


def matched_terms(text: str, app: AppSettings | None = None) -> list[Term]:
    """去重后的术语列表（按首次出现顺序）。"""
    seen: set[str] = set()
    result: list[Term] = []
    for match in match_terms(text, app):
        if match.term.id in seen:
            continue
        seen.add(match.term.id)
        result.append(match.term)
    return result


def _replace_terms(text: str, terms: list[Term], value_for) -> str:
    """按术语逐个替换；每个术语只扫一遍，避免替换结果被自己的其它写法再次命中。"""
    result = text
    for term in terms:
        patterns = "|".join(_surface_pattern(surface) for surface in term.match)
        value = value_for(term)
        result = re.sub(
            patterns,
            lambda _match, replacement=value: replacement,
            result,
            flags=re.IGNORECASE,
        )
    return result


def canonical_query(text: str, app: AppSettings | None = None) -> str:
    """把文本里的术语统一成词典里的规范写法；没有变化时返回空串。"""
    terms = matched_terms(text, app)
    if not terms:
        return ""
    rewritten = _replace_terms(text, terms, lambda term: term.term)
    return rewritten if rewritten != text else ""


def alias_queries(
    text: str, app: AppSettings | None = None, *, limit: int = 2
) -> list[str]:
    """生成别名写法，供首轮为空时扩一次检索；最多 ``limit`` 条。"""
    terms = matched_terms(text, app)
    if not terms:
        return []
    queries: list[str] = []
    for term in terms:
        for alias in term.aliases:
            variant = _replace_terms(text, [term], lambda _term, value=alias: value)
            if variant != text and variant not in queries:
                queries.append(variant)
            if len(queries) >= limit:
                return queries
    return queries


def filter_own_hits(
    question: str,
    hits: list[dict],
    app: AppSettings | None = None,
    *,
    terms: tuple[Term, ...] | None = None,
) -> list[dict]:
    """定义题术语门禁：只留下这条问题所问术语自己的证据。

    两条规则，刻意收窄：

    * 问题里有已知术语、且该术语有 ``kb_ref``：丢掉**其它术语词条**的命中，
      非术语内容（洗护/指南/档案）原样保留；
    * 术语没有 ``kb_ref``：只保留正文真的提到该术语的证据，防止「问 A 却
      命中 B 的词条」。

    只应作用于定义题（调用方判断），普通洗护/搭配题不受影响。
    """
    dictionary = load_terms(app) if terms is None else terms
    matched = _matched_from(question, dictionary)
    if not matched:
        return hits

    known_refs = {term.kb_ref for term in dictionary if term.kb_ref}
    allowed_refs = {term.kb_ref for term in matched if term.kb_ref}
    if allowed_refs:
        return [
            hit
            for hit in hits
            if (hit.get("chunk_id") or "") not in known_refs
            or (hit.get("chunk_id") or "") in allowed_refs
        ]

    surfaces = [_squash(surface) for term in matched for surface in term.match]
    kept: list[dict] = []
    for hit in hits:
        text = _squash(f"{hit.get('author') or ''} {hit.get('text') or ''}")
        if any(surface and surface in text for surface in surfaces):
            kept.append(hit)
    return kept


def _matched_from(question: str, terms: tuple[Term, ...]) -> list[Term]:
    normalized = _normalize(question)
    found: list[TermMatch] = []
    for term in terms:
        for surface in term.match:
            for match in re.finditer(_surface_pattern(surface), normalized):
                found.append(TermMatch(term, match.start(), match.end()))
    found.sort(key=lambda item: (-(item.end - item.start), item.start))
    chosen: list[TermMatch] = []
    for candidate in found:
        if all(
            candidate.end <= other.start or candidate.start >= other.end
            for other in chosen
        ):
            chosen.append(candidate)
    seen: set[str] = set()
    result: list[Term] = []
    for match in sorted(chosen, key=lambda item: item.start):
        if match.term.id in seen:
            continue
        seen.add(match.term.id)
        result.append(match.term)
    return result

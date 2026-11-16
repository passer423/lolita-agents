"""术语词典：匹配、规范改写与定义题门禁。

这些用例只用仓库里的 ``content/terms/lolita_terms.json``，不联网、不读索引，
确保「词典写错了/和 KB 脱节了」能在单元测试层面直接暴露。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import terms

ROOT = Path(__file__).resolve().parents[1]


def hit(ref: str, text: str = "内容") -> dict:
    return {"chunk_id": ref, "author": "知识库", "text": text}


def test_dictionary_entries_point_to_existing_kb_entries():
    dictionary = terms.load_terms()
    assert dictionary, "术语词典没有装载到任何条目"
    for term in dictionary:
        assert term.term and term.match
        assert term.kb_ref, f"{term.id} 没有 kb_ref"
        doc_id = term.kb_ref.split("#")[0]
        assert (ROOT / "content" / "kb" / f"{doc_id}.md").exists(), (
            f"{term.id} 的 kb_ref 指向了不存在的词条：{term.kb_ref}"
        )


def test_removed_terms_are_not_in_dictionary():
    """黑lo/白lo 已确认不作为 lolita 术语收录，防止以后又被加回来。"""
    names = {term.term for term in terms.load_terms()}
    assert "黑lo" not in names
    assert "白lo" not in names


@pytest.mark.parametrize("text, expected", [
    ("痛lo是什么意思", "痛lo"),
    ("痛 lo 是什么意思", "痛lo"),
    ("痛LO是什么意思", "痛lo"),
    ("痛 包 是什么", "痛包"),
    ("JSK 和 OP 有什么区别", "JSK"),
    ("jumper skirt 是什么", "JSK"),
    ("尾款什么时候补", "定金和尾款"),
])
def test_matches_variants(text, expected):
    assert expected in [term.term for term in terms.matched_terms(text)]


@pytest.mark.parametrize("text", ["open 是什么", "sky 是什么", "shopping 是什么"])
def test_latin_terms_require_word_boundary(text):
    """两三个字母的缩写不能当子串匹配，否则 "op" 会命中 "open"。"""
    assert terms.matched_terms(text) == []


def test_canonical_query_unifies_spacing_and_case():
    assert terms.canonical_query("痛 lo 是什么") == "痛lo 是什么"
    assert terms.canonical_query("痛LO是什么") == "痛lo是什么"
    assert terms.canonical_query("jumper skirt 是什么") == "JSK 是什么"
    assert terms.canonical_query("痛lo是什么") == ""


def test_alias_queries_do_not_rewrite_the_same_span_twice():
    aliases = terms.alias_queries("痛 lo 是什么")
    assert aliases
    assert all(alias.count("日常") <= 1 for alias in aliases)
    assert all(alias.count("轻装") <= 1 for alias in aliases)


def test_gate_drops_other_term_entries():
    """问痛包时，不能拿痛 lo 的词条当答案。"""
    result = terms.filter_own_hits("痛包是什么", [hit("term-tonglo#0")])
    assert result == []


def test_gate_keeps_the_asked_term_entry():
    kept = hit("term-tongbao#0")
    assert terms.filter_own_hits("痛包是什么", [kept]) == [kept]


def test_gate_keeps_non_term_content():
    """术语门禁只挡其它术语词条，不挡洗护/指南这类普通内容。"""
    kept = hit("care-lace#0")
    assert terms.filter_own_hits("痛lo是什么意思", [kept]) == [kept]


def test_gate_keeps_both_entries_in_comparison_question():
    """JSK 和 OP 共用一个对比词条，不能把其中任何一个当成「别的术语」。"""
    kept = [hit("term-jsk-op#0"), hit("term-tonglo#0")]
    result = terms.filter_own_hits("JSK 和 OP 有什么区别", kept)
    assert [item["chunk_id"] for item in result] == ["term-jsk-op#0"]


def test_gate_without_known_term_is_transparent():
    hits = [hit("care-lace#0"), hit("guide-newbie#0")]
    assert terms.filter_own_hits("蕾丝怎么洗", hits) == hits

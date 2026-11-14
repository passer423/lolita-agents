"""写作层：句级引用覆盖、补齐与来源归因。

覆盖校验本身**只告警**；补齐只发生在非流式，且只补证据真的支撑的那一句，
补不动时不许硬挂。归因必须跟着证据性质走——店家页面不能被写成
「有网友提到」。三件事都不联网：模型输出由假客户端给定。
"""

from __future__ import annotations

import pytest

from app.agent import graph, trace
from app.agent import writer as writer_mod
from app.agent.writer import (
    APPELLATION_SHOP,
    APPELLATION_UGC,
    DeepSeekWriter,
    build_synth_messages,
    fill_citations,
    fix_source_attribution,
    grounding_score,
    source_appellation,
    uncited_claim_sentences,
)
from app.config import settings

# ---------------------------------------------------------------- 假客户端


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeCompletions:
    def __init__(self, *, content: str = "", chunks: list[str] | None = None):
        self.content = content
        self.chunks = list(chunks or [])
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            stream = [
                _Obj(choices=[_Obj(delta=_Obj(content=piece))], usage=None)
                for piece in self.chunks
            ]
            stream.append(_Obj(choices=[], usage=_Obj(prompt_tokens=10, completion_tokens=5)))
            return iter(stream)
        return _Obj(
            choices=[_Obj(message=_Obj(content=self.content))],
            usage=_Obj(prompt_tokens=10, completion_tokens=5),
        )


def make_writer(*, content: str = "", chunks: list[str] | None = None) -> DeepSeekWriter:
    app = settings.model_copy(
        update={"llm_api_key": "test-key", "llm_base_url": "http://127.0.0.1:9/v1"}
    )
    writer = DeepSeekWriter(app)
    writer._client = _Obj(chat=_Obj(completions=_FakeCompletions(content=content, chunks=chunks)))
    return writer


def test_writer_uses_configured_max_tokens(monkeypatch):
    """上限必须来自配置：推理模型的推理 token 也占这个预算，600 会截断。"""
    monkeypatch.setattr(settings, "writer_max_tokens", 1800)
    writer = make_writer(content="[1] 可以。")
    writer.write("蕾丝怎么洗", evidence())

    call = writer._client.chat.completions.calls[-1]
    assert call["max_tokens"] == 1800
    assert writer._max_tokens == 1800


def test_stream_uses_configured_max_tokens(monkeypatch):
    monkeypatch.setattr(settings, "writer_max_tokens", 1600)
    writer = make_writer(chunks=["[1] 可以。"])
    list(writer.stream("蕾丝怎么洗", evidence()))

    call = writer._client.chat.completions.calls[-1]
    assert call["max_tokens"] == 1600


def evidence(**overrides) -> list[dict]:
    item = {
        "n": 1,
        "channel": "real",
        "source_kind": "upstream_ugc",
        "ref": "real-1",
        "title": "求助帖",
        "text": "内容",
        "url": "",
    }
    item.update(overrides)
    return [item]


STORE = {
    "n": 1,
    "channel": "real",
    "source_kind": "upstream_ugc",
    "ref": "real-02",
    "title": "圆点点小姐洋服社-练果子《珀茵特庄园质检报告出来啦……》",
    "text": "经过我们的水洗实验，使用普通洗涤剂手洗或机洗两次，裙子表面浮色基本掉完。",
    "url": "",
}


# ---------------------------------------------------------------- 句级覆盖


AUDIT_NOISE = [
    # real-02：免责
    "不过这只是网友的说法，具体怎么洗还是以店家或吊牌上的说明为准。",
    # real-04：免责 + 已说明没查到
    "不过这只是网友自己的说法，官方洗护说明我这边没查到。",
    # real-06：降权
    "这只能算个人感受，具体某条裙子还是得看那家的尺码表。",
    # lolibrary-01：元陈述
    "资料里只记了品牌、品类和年份，没有更多信息。",
    # lolibrary-08：元陈述
    "我这边只查到这条档案记录，没有别的细节。",
    # lolibrary-08：反问收尾
    "你想了解哪方面，可以说得具体点。",
]

REAL_GAP = (
    "JSK 是没袖子的吊带连衣裙，上身像背心，得配内搭衬衫或打底才能穿；"
    "OP 是带袖子的连体裙，一件直接上身。"
)


def test_only_real_gap_is_flagged_among_audit_sentences():
    """实测口径：6 句豁免噪声 + 1 句真缺口，只应命中真缺口（误报 0/6）。"""
    answer = REAL_GAP + "".join(AUDIT_NOISE)

    flagged = uncited_claim_sentences(answer, evidence())

    assert flagged == [REAL_GAP]


def test_semicolon_is_not_a_sentence_boundary():
    """写作器习惯把 [n] 挂在分号链末尾，切分号会把前半截误判成没引用。"""
    answer = "这条可以冷水手洗，但不要拧干，也不要暴晒 [1]；晾的时候平铺。"

    assert uncited_claim_sentences(answer, evidence()) == []


def test_short_sentence_is_not_a_claim():
    assert uncited_claim_sentences("会掉色。", evidence()) == []


def test_no_evidence_is_not_judged():
    """没有证据时回答本来就该是拒答，不能反过来要求引用。"""
    assert uncited_claim_sentences(REAL_GAP, []) == []


def test_coverage_warning_reaches_response_usage_without_touching_answer(monkeypatch):
    """告警走 writer.usage：graph 会把它同时写进响应与轨迹，答案文本不动。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)

    def recall(channel, question, ctx, *, query=""):
        if channel == "own":
            return evidence(channel="own", source_kind="own_content", title="洗护", text="冷水手洗")
        return []

    from app.agent import channels

    monkeypatch.setattr(channels, "recall", recall)
    answer = "这条裙子的浮色用普通洗涤剂手洗两次基本就掉完了。别用热水泡[1]。"
    writer = make_writer(content=answer)

    result = graph.run(
        "蕾丝怎么洗",
        ctx=channels.RecallContext(settings=settings),
        planner=_Obj(usage={}),
        writer=writer,
    )

    diagnostics = result["usage"]["writer"]["diagnostics"]
    assert result["answer"] == answer
    # 顶层字段给前端和排查直接用，不再要求先翻 usage。
    assert result["citation_coverage"]["uncited_count"] == 1
    assert diagnostics["citation_coverage"]["uncited_count"] == 1
    assert result["citation_coverage"]["uncited_sentences"] == [
        "这条裙子的浮色用普通洗涤剂手洗两次基本就掉完了。"
    ]


def test_stream_warns_but_does_not_rewrite_answer():
    """流式只记账不替换：用户已经逐段读过，不能回头换掉。"""
    answer = "这条裙子的浮色用普通洗涤剂手洗两次基本就掉完了。"
    writer = make_writer(chunks=["这条裙子的浮色用普通洗涤剂", "手洗两次基本就掉完了。"])

    text = "".join(writer.stream("q", evidence()))

    assert text == answer
    diagnostics = writer.usage["diagnostics"]
    assert diagnostics["streamed"] is True
    assert diagnostics["citation_coverage"]["uncited_count"] == 1


# ---------------------------------------------------------------- 句级引用补齐


# own-08 的原句：证据（自有知识库）逐条写着 JSK / OP 的定义，只是模型没在这句标编号。
OWN_DEFINITION = (
    "JSK 是没袖子的吊带连衣裙，上身像背心，得配内搭衬衫或打底才能穿；"
    "OP 是带袖子的连体裙，一件直接上身。"
)
OWN_EVIDENCE = [
    {
        "n": 1,
        "channel": "own",
        "source_kind": "own_content",
        "ref": "term-jskop#0",
        "title": "知识库",
        "text": (
            "JSK 和 OP 有什么区别\n"
            "JSK 是 Jumper Skirt 的缩写，指没有袖子的吊带连衣裙，上身是背心式，"
            "需要搭配内搭衬衫或打底才能穿。\n"
            "OP 是 One-Piece 的缩写，指带袖子的连体连衣裙，一件直接上身。"
        ),
        "url": "",
    }
]


def test_sentence_is_filled_when_evidence_really_supports_it():
    """own-08：编号补在句末标点前，和模型自己写的「… [1]。」同形。"""
    writer = make_writer(content=OWN_DEFINITION)
    text = writer.write("JSK 和 OP 有什么区别", OWN_EVIDENCE)

    assert text == OWN_DEFINITION[:-1] + " [1]。"
    coverage = writer.usage["diagnostics"]["citation_coverage"]
    assert coverage == {
        "checked_evidence": 1,
        "uncited_count": 0,
        "uncited_sentences": [],
        "filled_count": 1,
    }
    assert writer.usage["diagnostics"]["citation_fills"] == [
        {"sentence": OWN_DEFINITION, "n": 1}
    ]


def test_advice_sentence_is_not_given_a_topic_neighbour_citation():
    """real-11：求助帖只是问了同一件事，不能当成「要细节图」这条建议的依据。"""
    ask = evidence(
        channel="real",
        ref="real-11#0",
        title="lo娘发言",
        text="想问问收别人的lo裙有什么要注意的吗，需不需要让对方拍细节图呢",
    )
    sentence = "要细节图，而且得让对方拍实拍，不是店家图。"

    text, filled, narrowed = fill_citations(sentence, ask)

    assert text == sentence
    assert filled == []
    assert narrowed == []


def test_number_mismatch_blocks_fill():
    """价格/日期这类数字对不上，就算字面重合度高也不挂。"""
    ev = evidence(
        channel="own",
        source_kind="own_content",
        text="定金 188，尾款 390，两周内付清。",
    )
    sentence = "定金 188，尾款 390，全款 578 元。"

    assert grounding_score(sentence, ev[0]["text"]) >= writer_mod.MIN_GROUNDING
    assert fill_citations(sentence, ev)[1] == []


def test_definition_without_any_evidence_is_narrowed_not_cited():
    """lolibrary-03：定义句只有标题级的档案，挂 [2] 等于替档案编定义。"""
    ev = evidence(
        channel="lolibrary",
        source_kind="third_party",
        title="White Lily OP (2026)",
        text="Innocent World｜OP",
        url="https://lolibrary.org/items/x",
    )
    sentence = "OP 就是 one-piece 的缩写，指连身裙；整件式，不用另配上衣。"

    text, filled, narrowed = fill_citations(sentence, ev)

    assert text == "按一般说法，" + sentence
    assert filled == []
    assert narrowed == [sentence]


def test_stream_does_not_fill_citations():
    """流式的字已经交付，补编号等于读者回头看时句子变了。"""
    writer = make_writer(chunks=[OWN_DEFINITION])

    text = "".join(writer.stream("JSK 和 OP 有什么区别", OWN_EVIDENCE))

    assert text == OWN_DEFINITION
    assert writer.usage["diagnostics"]["citation_coverage"]["filled_count"] == 0


def test_store_meta_sentence_is_neither_cited_nor_rewritten():
    """real-02：说明来源性质的那句没有可挂的证据，归因照旧只改称谓。"""
    writer = make_writer(
        content="有网友提到，用普通洗涤剂手洗两次浮色就掉完了[1]。"
        "这是店家的说法，不是质检机构的结论。"
    )

    text = writer.write("这条掉色吗", [{**STORE}])

    assert text == (
        "店家提到，用普通洗涤剂手洗两次浮色就掉完了[1]。"
        "这是店家的说法，不是质检机构的结论。"
    )
    assert writer.usage["diagnostics"]["citation_fills"] == []


# ---------------------------------------------------------------- 来源归因


def test_store_post_is_appellation_not_netizen():
    """real-02：店家的质检说明不能写成「有网友提到」。"""
    ev = [{**STORE}]
    assert source_appellation(ev[0]) == APPELLATION_SHOP

    prompt = build_synth_messages("这条掉色吗", ev)[-1]["content"]
    assert "（店家/商家）" in prompt

    writer = make_writer(content="有网友提到，用普通洗涤剂手洗两次浮色就掉完了[1]。")
    text = writer.write("这条掉色吗", ev)

    assert "网友" not in text
    assert "店家提到" in text
    assert writer.usage["diagnostics"]["source_attribution_fixes"]


def test_real_ugc_keeps_netizen_wording():
    ev = evidence(title="某买家发帖", text="我机洗没事")
    assert source_appellation(ev[0]) == APPELLATION_UGC

    writer = make_writer(content="有网友提到，她机洗没事[1]。")

    assert writer.write("能机洗吗", ev) == "有网友提到，她机洗没事[1]。"


def test_archive_is_not_called_netizen():
    """档案是社区整理，不是品牌官方——既不能叫网友，也不能抬成官方。"""
    ev = evidence(source_kind="third_party", title="Memorial Cake JSK", url="https://lolibrary.org/items/x")
    assert source_appellation(ev[0]) == "档案（Lolibrary）"

    writer = make_writer(content="有网友说这条是 2016 年的款式[1]。")
    text = writer.write("哪年的", ev)

    assert "网友" not in text
    assert "官方" not in text
    assert text == "Lolibrary 上说这条是 2016 年的款式[1]。"


def test_community_archive_without_lolibrary_is_still_archive():
    ev = evidence(source_kind="third_party", title="某品牌年鉴摘录", url="https://example.com/a")
    assert source_appellation(ev[0]) == "档案（社区整理）"

    writer = make_writer(content="有网友提到这条是早年款[1]。")

    assert writer.write("哪年的", ev) == "档案记录里提到这条是早年款[1]。"


def test_official_is_reserved_for_brand_channels():
    ev = evidence(source_kind="realtime", title="品牌官方公告：2026 年再贩", text="6 月 1 日开售")
    assert source_appellation(ev[0]) == "官方"

    writer = make_writer(content="有网友说 6 月 1 日开售[1]。")

    assert writer.write("什么时候开售", ev) == "官方说 6 月 1 日开售[1]。"


def test_archive_is_not_promoted_to_official_in_prompt():
    archive = evidence(
        source_kind="third_party", title="Memorial Cake JSK", url="https://lolibrary.org/items/x"
    )
    messages = build_synth_messages("q", archive)

    assert "（档案（Lolibrary））" in messages[-1]["content"]
    assert "社区整理" in messages[0]["content"]
    assert "不是品牌官方" in messages[0]["content"]


def test_mixed_ugc_and_store_citation_is_left_alone():
    """同一句既引了网友又引了店家：分不清「网友」指谁，宁可不改。"""
    ev = [
        {**STORE, "n": 1},
        {"n": 2, "channel": "real", "source_kind": "upstream_ugc",
         "ref": "real-2", "title": "买家发帖", "text": "我洗过", "url": ""},
    ]
    answer = "有网友提到，洗两次就掉完了[1][2]。"

    text, fixes = fix_source_attribution(answer, ev)

    assert text == answer
    assert fixes == []


def test_prompt_forbids_calling_shop_netizen():
    messages = build_synth_messages("q", [{**STORE}])
    system = messages[0]["content"]

    assert "禁止用「网友说」指代店家、官方或档案" in system
    assert "绝不能写成「网友」" in messages[-1]["content"]


@pytest.mark.parametrize("url, expected", [
    ("https://item.taobao.com/item.htm?id=1", APPELLATION_SHOP),
    ("https://www.xiaohongshu.com/explore/1", APPELLATION_UGC),
])
def test_shop_domain_wins_even_for_ugc_kind(url, expected):
    ev = evidence(title="某条裙子", text="现货", url=url)
    assert source_appellation(ev[0]) == expected


def test_writer_modules_exports_are_importable():
    """防止以后重构把公共入口改名却不更新调用方。"""
    assert writer_mod.APPELLATION_SHOP == APPELLATION_SHOP


# ---------------------------------------------------------------- 图片兜底提示


def _image_evidence(**overrides) -> list[dict]:
    return evidence(image_count=18, image_urls=["https://img/1", "https://img/2"], **overrides)


def test_evidence_without_image_gets_no_hint():
    """微博/网页证据没有 image_count，一个字的提示都不该多。"""
    writer = make_writer(content="[1] 这条要等再贩。")

    assert writer.write("多少钱", evidence()) == "[1] 这条要等再贩。"


def test_hint_only_for_price_or_size_questions():
    writer = make_writer(content="[1] 这条很受欢迎。")

    assert writer.write("这条好看吗", _image_evidence()) == "[1] 这条很受欢迎。"


def test_hint_skipped_when_text_already_has_price():
    """正文已经给出价格数字时不必再说「图里可能有」。"""
    ev = _image_evidence(text="预约价 578，定金 188")
    writer = make_writer(content="[1] 预约价 578。")

    assert writer.write("四时花价格", ev) == "[1] 预约价 578。"


def test_price_question_with_images_appends_fixed_template_once():
    ev = [
        {"n": 1, **{k: v for k, v in _image_evidence()[0].items() if k != "n"}},
        {"n": 2, "channel": "xhs", "source_kind": "realtime", "ref": "x",
         "title": "另一帖", "text": "无正文", "url": "", "image_count": 9, "image_urls": []},
    ]
    writer = make_writer(content="[1] 目前没有明确价格。")

    text = writer.write("四时花价格", ev)

    assert text.startswith("[1] 目前没有明确价格。")
    assert "[1] 那条笔记含 18 张图" in text
    assert "可能含价格表" in text
    assert "以原帖图片为准" in text
    # 全答案最多一条：两条带图证据也只出现一次「为准」
    assert text.count("为准") == 1


def test_hint_points_at_the_price_table_post():
    """多条带图证据时，提示要挂在标题写了「价格表」的那条，而不是第一条。"""
    ev = [
        {"n": 1, "channel": "xhs", "source_kind": "realtime", "ref": "a",
         "title": "纠结可能再贩", "text": "无正文", "url": "", "image_count": 2, "image_urls": []},
        {"n": 2, "channel": "xhs", "source_kind": "realtime", "ref": "b",
         "title": "一人血书求四时花再贩（存档价格表", "text": "无正文", "url": "",
         "image_count": 2, "image_urls": []},
    ]
    writer = make_writer(content="[1] 没查到确切数字。")

    text = writer.write("四时花价格", ev)

    assert "[2] 那条笔记含 2 张图" in text
    assert "[1] 那条笔记" not in text


def test_size_question_uses_size_template():
    writer = make_writer(content="[1] 资料没写尺码。")

    text = writer.write("M 码胸围多少", _image_evidence())

    assert "可能含尺码表" in text
    assert "尺码请以原帖图片为准" in text


def test_stream_appends_hint_at_end_once():
    writer = make_writer(chunks=["[1] 没有明确价格。"])

    text = "".join(writer.stream("四时花价格", _image_evidence()))

    assert text.endswith("价格请以原帖图片为准。")
    assert text.count("为准") == 1

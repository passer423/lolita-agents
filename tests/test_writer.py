"""写作层：句级引用覆盖告警与来源归因。

覆盖校验**只告警、不改答案**；归因必须跟着证据性质走——店家页面不能被写成
「有网友提到」。两件事都不联网：模型输出由假客户端给定。
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
    fix_source_attribution,
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

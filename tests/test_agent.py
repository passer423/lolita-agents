"""agent 的多路召回、编排护栏与协议测试。

这一层出错的代价是"给出错误答案"或"烧钱"，所以每条护栏单独钉死：

* 召回通道一源一通道、失败即跳过不猜；
* 编排：核心通道并行召回、planner 单次决定补查、没证据不许作答、引用越界降级；
* 轨迹写盘。

**不联网、不需要 key**：planner 是脚本化的，通道用假实现替掉，
模型行为由用例决定。
"""

from __future__ import annotations

import json
import time

import pytest

from app.agent import channels, graph, trace
from app.agent.channels import ChannelUnavailable, RecallContext
from app.agent.planner import PlannerOutputError, build_query_prompt, parse_query, plan_channels
from app.config import AppSettings, settings
from app.services import lolibrary_online

# --------------------------------------------------------------------- 脚本化依赖


class ScriptedPlanner:
    """按脚本返回补查决策；脚本用完就不再补查（避免测试挂死）。"""

    def __init__(self, script=None, dress_script=None):
        self.script = list(script or [])
        self.dress_script = list(dress_script or [])
        self.calls = 0
        self.dress_calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def extract_query(self, question, history=None):
        self.calls += 1
        self.usage["calls"] = self.calls
        if not self.script:
            return question
        return self.script.pop(0)

    def extract_dress(self, question, history=None):
        self.dress_calls += 1
        if not self.dress_script:
            return {"brand": "", "region": "", "series": "", "series_cn": "", "confidence": "low"}
        return self.dress_script.pop(0)


class StubWriter:
    usage = {"calls": 1}

    def write(self, question, evidence, history=None, **_kwargs):
        return "[1] 装洗衣袋。"

    def stream(self, question, evidence, history=None, **_kwargs):
        yield "蕾丝"
        yield "要装洗衣袋[1]。"


def ctx() -> RecallContext:
    return RecallContext(settings=settings)


def evidence(channel, ref=None, title=None, text="内容", url="", source_kind=None):
    return [{
        "channel": channel,
        "source_kind": source_kind or channels.CHANNEL_SOURCE_KIND[channel],
        "ref": ref or channel,
        "title": title or channel,
        "text": text,
        "url": url,
    }]


def fake_recall(channels_map):
    """按通道名返回假证据；值为 Exception 时抛 ChannelUnavailable 语义。"""

    def recall(channel, question, ctx, *, query=""):
        result = channels_map.get(channel)
        if isinstance(result, Exception):
            raise result
        return list(result or [])

    return recall


def core_only(monkeypatch):
    """核心通道有货、补查通道都不开。"""
    monkeypatch.setattr(
        channels, "recall",
        fake_recall({
            "own": evidence("own"),
            "real": evidence("real"),
            "lolibrary": evidence("lolibrary"),
        }),
    )


# --------------------------------------------------------------------- 协议解析


@pytest.mark.parametrize("question, expected", [
    ("这条裙子多少钱", ["xhs", "web"]),
    ("有实拍测评吗", ["xhs", "weibo"]),
    ("最近有什么新款", ["web"]),
    ("价格和测评都想知道", ["xhs", "weibo", "web"]),
    ("蕾丝怎么洗", []),
])
def test_plan_channels_by_intent(question, expected):
    """补查决策是纯规则：不看召回结果，只看问题命中了哪类实时意图。"""
    assert plan_channels(question) == expected


@pytest.mark.parametrize("text, expected", [
    ('{"query":"Baby 缎带"}', "Baby 缎带"),
    ('{"query":"  四时花  "}', "四时花"),
])
def test_parse_query_accepts_valid(text, expected):
    assert parse_query(text) == expected


@pytest.mark.parametrize("text, fragment", [
    ("不是 JSON", "合法 JSON"),
    ('{"query":123}', "非空字符串"),
    ('{"query":"   "}', "非空字符串"),
    ('["数组"]', "JSON 对象"),
])
def test_parse_query_rejects_invalid(text, fragment):
    with pytest.raises(PlannerOutputError) as exc:
        parse_query(text)
    assert fragment in str(exc.value)


def test_query_prompt_carries_history_for_pronouns():
    """「这条多少钱」里的「这条」只能靠历史解开。"""
    prompt = build_query_prompt("这条多少钱", [
        {"role": "user", "content": "Baby 秋日私语"},
        {"role": "assistant", "content": "是 2026 秋冬款"},
    ])
    assert "Baby 秋日私语" in prompt
    assert "指代" in prompt


# --------------------------------------------------------------------- 编排护栏


def test_core_channels_recalled_in_parallel(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    planner = ScriptedPlanner([])

    result = graph.run("四时花怎么洗", ctx=ctx(), planner=planner, writer=StubWriter())

    channels_seen = {item["channel"] for item in result["evidence"]}
    assert channels_seen == {"own", "real", "lolibrary"}
    assert [item["n"] for item in result["evidence"]] == [1, 2, 3]
    assert result["refused"] is False
    # 不需要补查时，模型一次都不该被调用 —— 这是拆分「规则 / 模型」的核心收益。
    assert planner.calls == 0


def test_supplementary_channels_decided_by_rule(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(
        channels, "recall",
        fake_recall({
            "own": evidence("own"),
            "real": evidence("real"),
            "lolibrary": evidence("lolibrary"),
            "xhs": evidence("xhs", url="https://xhs/1"),
        }),
    )
    planner = ScriptedPlanner(["预约价怎么算"])

    result = graph.run("预约价怎么算", ctx=ctx(), planner=planner, writer=StubWriter())

    seen = {item["channel"] for item in result["evidence"]}
    assert "xhs" in seen
    # 规则命中「预约」→ 交易类 → 小红书 + 网页，与模型怎么说无关
    assert any(
        step["kind"] == "route" and step["supplementary"] == ["xhs", "web"]
        for step in result["steps"]
    )
    assert planner.calls == 1


# --------------------------------------------------------------------- 裙子档案


def test_dress_jp_only_queries_lolibrary(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    calls: list[tuple[str, str]] = []

    def recall(channel, question, ctx, *, query=""):
        calls.append((channel, query))
        return evidence(channel) if channel in ("own", "real", "lolibrary") else []

    monkeypatch.setattr(channels, "recall", recall)
    app = settings.model_copy(update={"lolibrary_online_enabled": False})
    result = graph.run(
        "皇家千层公主什么时候发售",
        ctx=RecallContext(settings=app),
        planner=ScriptedPlanner([]),
        writer=StubWriter(),
    )

    assert not [channel for channel, _ in calls if channel in ("xhs", "weibo", "web")]
    lolibrary_query = next(query for channel, query in calls if channel == "lolibrary")
    assert "Mille-feuille Royal pour la Princesse" in lolibrary_query
    assert "Baby" not in lolibrary_query
    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["supplementary"] == []
    assert route["dress_region"] == "JP"
    assert result["dress"]["brand"] == "Baby, the Stars Shine Bright"
    assert result["dress"]["region"] == "JP"


def test_dress_cn_forces_xhs_and_weibo(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    calls: list[tuple[str, str]] = []

    def recall(channel, question, ctx, *, query=""):
        calls.append((channel, query))
        if channel in ("own", "real", "lolibrary"):
            return evidence(channel)
        if channel in ("xhs", "weibo"):
            return evidence(channel, text="四时花预约价 ¥398，2026年5月发售，粉色/黑色")
        return []

    monkeypatch.setattr(channels, "recall", recall)
    planner = ScriptedPlanner([])
    result = graph.run(
        "四时花 预约价是多少",
        ctx=ctx(),
        planner=planner,
        writer=StubWriter(),
    )

    assert any(channel == "xhs" for channel, _ in calls)
    assert any(channel == "weibo" for channel, _ in calls)
    assert not any(channel == "web" for channel, _ in calls)
    social_queries = [query for channel, query in calls if channel == "xhs"]
    assert social_queries and social_queries[0].startswith("四时花")
    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["supplementary"] == ["xhs", "weibo"]
    assert route["dress_region"] == "CN"
    assert "398" in result["dress"]["price"]
    # 已经识别出中文系列名，不再花一次模型调用提检索词。
    assert planner.calls == 0


def test_dress_unknown_region_queries_both(monkeypatch, workdir):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(json.dumps({
        "brands": [{"id": "mystery", "name": "Mystery Brand", "region": "UNKNOWN",
                    "aliases": ["Mystery"]}],
    }, ensure_ascii=False), encoding="utf-8")
    app.dress_aliases_path.write_text(json.dumps({"aliases": []}), encoding="utf-8")
    app.lolibrary_catalog_path.write_text("", encoding="utf-8")

    calls: list[str] = []

    def recall(channel, question, ctx, *, query=""):
        calls.append(channel)
        return evidence(channel) if channel != "xhs" else evidence(channel, text="价格 398元")

    monkeypatch.setattr(channels, "recall", recall)
    result = graph.run(
        "Mystery 的裙子多少钱",
        ctx=RecallContext(settings=app),
        planner=ScriptedPlanner([]),
        writer=StubWriter(),
    )

    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["supplementary"] == ["xhs", "weibo", "web"]
    assert route["dress_region"] == "UNKNOWN"
    assert {"xhs", "weibo", "web"} <= set(calls)
    assert result["dress"]["region"] == "UNKNOWN"


def test_dress_model_hint_fills_series(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({
        "own": evidence("own"), "real": evidence("real"), "lolibrary": evidence("lolibrary"),
    }))
    planner = ScriptedPlanner([], dress_script=[{
        "brand": "Baby, the Stars Shine Bright",
        "region": "JP",
        "series": "Mille-feuille Royal pour la Princesse",
        "series_cn": "皇家千层公主",
        "confidence": "high",
    }])
    app = settings.model_copy(update={"lolibrary_online_enabled": False})

    result = graph.run(
        "Baby 缎带与齿轮 预约价多少",
        ctx=RecallContext(settings=app),
        planner=planner,
        writer=StubWriter(),
    )

    assert planner.dress_calls == 1
    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert "Mille-feuille Royal pour la Princesse" in route["dress_series"]
    assert route["dress_region"] == "JP"


def test_pending_dress_question_uses_model_and_verifies(monkeypatch, workdir):
    """本地认不出的中文名：模型兜底 → Lolibrary 校验 → 学到的别名落盘。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(json.dumps({
        "brands": [{"id": "baby", "name": "Baby, the Stars Shine Bright",
                    "region": "JP", "aliases": ["Baby"]}],
    }, ensure_ascii=False), encoding="utf-8")
    app.dress_aliases_path.write_text(json.dumps({"aliases": []}), encoding="utf-8")
    app.lolibrary_catalog_path.write_text("", encoding="utf-8")

    detail = lolibrary_online.ArchiveDetail(
        url="https://lolibrary.org/items/starry-night-jsk",
        title="Starry Night Dream JSK",
        brand="Baby, the Stars Shine Bright",
        category="JSK",
        product_number="B1JS1",
        release_year="2024",
        original_price="¥30000 JPY",
        colors=("Pink", "Black"),
    )
    monkeypatch.setattr(lolibrary_online, "lookup", lambda *a, **k: ([detail], ""))
    monkeypatch.setattr(channels, "recall", fake_recall({
        "own": evidence("own"), "real": evidence("real"), "lolibrary": evidence("lolibrary"),
    }))
    planner = ScriptedPlanner([], dress_script=[{
        "brand": "Baby, the Stars Shine Bright",
        "region": "JP",
        "series": "Starry Night Dream",
        "series_cn": "星月夜之梦",
        "confidence": "high",
    }])

    result = graph.run(
        "星月夜之梦价格",
        ctx=RecallContext(settings=app),
        planner=planner,
        writer=StubWriter(),
    )

    assert planner.dress_calls == 1
    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["supplementary"] == []
    assert route["dress_series"] == "Starry Night Dream"
    assert route["dress_source"] == "model"
    assert result["dress"]["verified"] is True
    assert result["dress"]["source"] == "model_verified"
    assert result["dress"]["release"] == "2024"
    learned = json.loads(app.learned_aliases_path.read_text(encoding="utf-8"))["aliases"]
    assert any(item["cn"] == "星月夜之梦" for item in learned)


def test_pending_dress_question_without_model_result_asks(monkeypatch):
    """模型也认不出时反问用户，别让普通通道拿另一条裙子顶替。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({
        "own": evidence("own"), "real": evidence("real"), "lolibrary": evidence("lolibrary"),
        "xhs": evidence("xhs"), "web": evidence("web"),
    }))
    planner = ScriptedPlanner([], dress_script=[{"confidence": "low"}])

    result = graph.run(
        "星月夜之梦价格",
        ctx=ctx(),
        planner=planner,
        writer=StubWriter(),
    )

    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["supplementary"] == []
    assert route["dress_unresolved"] is True
    assert result["dress"] is None
    assert "没认出" in result["answer"]
    assert "补充品牌" in result["answer"]
    assert result["stopped_reason"] == "dress_unresolved"
    assert not any(step["kind"] == "dress_profile" for step in result["steps"])


def test_glossary_search_resolves_before_model(monkeypatch, workdir):
    """拆词检索能命中时，不再花一次模型调用。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(json.dumps({
        "brands": [{"id": "baby", "name": "Baby, the Stars Shine Bright",
                    "region": "JP", "aliases": ["Baby"]}],
    }, ensure_ascii=False), encoding="utf-8")
    app.dress_aliases_path.write_text(json.dumps({"aliases": []}), encoding="utf-8")
    app.dress_glossary_path.write_text(json.dumps({
        "terms": [
            {"cn": "草莓", "en": ["strawberry"]},
            {"cn": "奶油", "en": ["cream"]},
            {"cn": "兔", "en": ["rabbit"]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    app.lolibrary_catalog_path.write_text("", encoding="utf-8")

    detail = lolibrary_online.ArchiveDetail(
        url="https://lolibrary.org/items/strawberry-cream-rabbit-jsk",
        title="Strawberry Cream Rabbit JSK",
        brand="Baby, the Stars Shine Bright",
        category="JSK",
        release_year="2024",
        original_price="¥30000 JPY",
        colors=("Pink",),
    )
    monkeypatch.setattr(lolibrary_online, "lookup", lambda *a, **k: ([detail], ""))
    monkeypatch.setattr(channels, "recall", fake_recall({
        "own": evidence("own"), "real": evidence("real"), "lolibrary": evidence("lolibrary"),
    }))
    planner = ScriptedPlanner([], dress_script=[{"confidence": "low"}])

    result = graph.run(
        "草莓奶油兔价格",
        ctx=RecallContext(settings=app),
        planner=planner,
        writer=StubWriter(),
    )

    assert planner.dress_calls == 0
    route = next(step for step in result["steps"] if step["kind"] == "route")
    assert route["dress_source"] == "glossary_verified"
    assert result["dress"]["verified"] is True
    assert result["dress"]["title"] == "strawberry cream rabbit"
    search_steps = [step for step in result["steps"] if step["kind"] == "dress_search"]
    assert search_steps and search_steps[0].get("matched_title")


def test_unverified_model_result_asks_for_confirmation(monkeypatch, workdir):
    """模型猜了系列但档案没核实：先反问，不把价格当事实。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(json.dumps({
        "brands": [{"id": "baby", "name": "Baby, the Stars Shine Bright",
                    "region": "JP", "aliases": ["Baby"]}],
    }, ensure_ascii=False), encoding="utf-8")
    app.dress_aliases_path.write_text(json.dumps({"aliases": []}), encoding="utf-8")
    app.lolibrary_catalog_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        lolibrary_online, "lookup", lambda *a, **k: ([], "Lolibrary 没有搜到裙子条目")
    )
    monkeypatch.setattr(channels, "recall", fake_recall({
        "own": evidence("own"), "real": evidence("real"), "lolibrary": evidence("lolibrary"),
    }))
    planner = ScriptedPlanner([], dress_script=[{
        "brand": "Baby, the Stars Shine Bright",
        "region": "JP",
        "series": "Starry Night Dream",
        "series_cn": "星月夜之梦",
        "confidence": "high",
    }])

    result = graph.run(
        "星月夜之梦价格",
        ctx=RecallContext(settings=app),
        planner=planner,
        writer=StubWriter(),
    )

    assert result["dress"]["verified"] is False
    assert result["dress"]["source"] == "model"
    assert "没核实到" in result["answer"]
    assert "补充品牌" in result["answer"]
    assert result["stopped_reason"] == "dress_unverified"


def test_no_evidence_is_refused(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({}))
    planner = ScriptedPlanner([])

    result = graph.run("有吗", ctx=ctx(), planner=planner)

    assert result["refused"] is True
    assert "不猜" in result["answer"]
    assert result["stopped_reason"] == "no_evidence"


def test_channel_unavailable_is_skipped_not_guessed(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(
        channels, "recall",
        fake_recall({
            "own": evidence("own"),
            "real": evidence("real"),
            "lolibrary": evidence("lolibrary"),
            "xhs": ChannelUnavailable("小红书未登录"),
        }),
    )
    planner = ScriptedPlanner([])

    result = graph.run("四时花 预约价", ctx=ctx(), planner=planner, writer=StubWriter())

    assert "xhs" not in {item["channel"] for item in result["evidence"]}
    assert any(step["kind"] == "skip" and step["channel"] == "xhs" for step in result["steps"])


def test_query_extraction_failure_still_supplements(monkeypatch):
    """提检索词失败不该阻止补查：查不查由规则决定，模型只优化检索词。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BrokenPlanner:
        usage = {}

        def extract_query(self, question, history=None):
            raise PlannerOutputError("模型没有返回合法 JSON")

    result = graph.run("预约价怎么算", ctx=ctx(), planner=BrokenPlanner(), writer=StubWriter())

    assert result["refused"] is False
    route_steps = [step for step in result["steps"] if step["kind"] == "route"]
    assert route_steps and route_steps[0]["supplementary"] == ["xhs", "web"]
    assert route_steps[0]["error"]


def test_out_of_range_citation_is_downgraded(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BadWriter:
        usage = {}

        def write(self, question, evidence, history=None, **_kwargs):
            return "有 [9] 条。"

    result = graph.run("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=BadWriter())

    assert "引用编号对不上" in result["answer"]
    assert result["stopped_reason"] == "citation_out_of_range"


def test_refusal_with_evidence_is_kept(monkeypatch):
    """模型说"没找到"时本来就不该有引用，不该被硬判成错误。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class RefusingWriter:
        usage = {}

        def write(self, question, evidence, history=None, **_kwargs):
            return "没有找到关于真丝的规定。"

    result = graph.run("真丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([]), writer=RefusingWriter())

    assert result["answer"] == "没有找到关于真丝的规定。"
    assert result["stopped_reason"] != "no_citation"


# --------------------------------------------------------------------- 流式


def test_stream_emits_steps_then_delta_then_final(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    events = list(graph.stream("蕾丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([]), writer=StubWriter()))

    kinds = [event["type"] for event in events]
    assert kinds[0] == "step"
    assert "delta" in kinds
    assert kinds[-1] == "final"
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    assert text == "蕾丝要装洗衣袋[1]。"
    assert events[-1]["answer"] == "蕾丝要装洗衣袋[1]。"


def test_stream_marks_bad_citation_instead_of_replacing(monkeypatch):
    """流式时用户已经读完了整段，不能再整段换掉——越界编号交给前端就地标出。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BadWriter:
        usage = {}

        def stream(self, question, evidence, history=None, **_kwargs):
            yield "有 [7] 条，还有 [1] 条。"

    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=BadWriter()))

    assert not any(event["type"] == "replace" for event in events)
    final = events[-1]
    assert final["answer"] == "有 [7] 条，还有 [1] 条。"
    assert final["invalid_citations"] == [7]
    assert final["citation_problems"]
    assert "citation_problems" in final["stopped_reason"]


def test_same_bad_citation_handled_by_delivery_mode(monkeypatch):
    """同一条越界引用：非流式换原文清单，流式保留并标红。

    这个分叉是交付方式决定的（非流式还没交付、流式已经逐字显示），
    不是实现走样——用一条用例把两边同时钉住，防止以后顺手"统一"掉。
    """
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class ForkWriter:
        usage = {}

        def write(self, question, evidence, history=None, **_kwargs):
            return "有 [7] 条。"

        def stream(self, question, evidence, history=None, **_kwargs):
            yield "有 [7] 条。"

    result = graph.run("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=ForkWriter())
    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=ForkWriter()))

    assert result["answer"] != "有 [7] 条。"
    assert result["stopped_reason"] == "citation_out_of_range"
    assert events[-1]["answer"] == "有 [7] 条。"
    assert events[-1]["invalid_citations"] == [7]


def test_stream_flags_answer_without_any_citation(monkeypatch):
    """有证据却整段没标来源：标不出来是"哪一条"，但也不能当成没问题。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class NoCiteWriter:
        usage = {}

        def stream(self, question, evidence, history=None, **_kwargs):
            yield "蕾丝要装洗衣袋。"

    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=NoCiteWriter()))

    final = events[-1]
    assert final["answer"] == "蕾丝要装洗衣袋。"
    assert final["invalid_citations"] == []
    assert any("没有标注来源" in item for item in final["citation_problems"])


def test_stream_refuses_without_evidence(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({}))
    events = list(graph.stream("有吗", ctx=ctx(), planner=ScriptedPlanner([])))

    assert events[-1]["refused"] is True
    assert "不猜" in events[-1]["answer"]


def test_stream_writer_failure_falls_back(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class DeadWriter:
        usage = {}

        def stream(self, *_args, **_kwargs):
            raise RuntimeError("上游 502")

    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([]), writer=DeadWriter()))

    assert any(event["type"] == "replace" and event["reason"] == "writer_failed" for event in events)
    assert events[-1]["answer"]


# --------------------------------------------------------------------- 整体预算


def deadline_ctx(seconds):
    return RecallContext(settings=settings.model_copy(update={"agent_deadline": seconds}))


def stalled_xhs(monkeypatch, stall=1.5):
    """核心通道与 web 立刻返回，xhs 卡住：用来验证「到点不等人」。"""

    def recall(channel, question, ctx, *, query=""):
        if channel == "xhs":
            time.sleep(stall)
            return []
        return evidence(channel) if channel in {"own", "real", "lolibrary", "web", "weibo"} else []

    monkeypatch.setattr(channels, "recall", recall)


def test_run_deadline_returns_partial_evidence(monkeypatch):
    """整体预算用尽：返回已到的证据 + 超时说明，不抛错（路由层才不会 500）。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    stalled_xhs(monkeypatch)

    result = graph.run(
        "预约价怎么算", ctx=deadline_ctx(0.4),
        planner=ScriptedPlanner(["预约价怎么算"]), writer=StubWriter(),
    )

    assert result["stopped_reason"] == "deadline"
    # 同一步里已经返回的 web 证据不能因为 xhs 卡住而被扣住
    assert {item["channel"] for item in result["evidence"]} == {"own", "real", "lolibrary", "web"}
    timeout_steps = [step for step in result["steps"] if step["kind"] == "timeout"]
    assert timeout_steps and "xhs" in timeout_steps[0]["pending"]
    assert "部分通道超时" in result["answer"]
    assert result["refused"] is False


def test_stream_deadline_returns_partial_then_final(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    stalled_xhs(monkeypatch)

    events = list(graph.stream(
        "预约价怎么算", ctx=deadline_ctx(0.4),
        planner=ScriptedPlanner(["预约价怎么算"]), writer=StubWriter(),
    ))

    assert any(event["type"] == "step" and event["kind"] == "timeout" for event in events)
    assert any(event["type"] == "replace" and event["reason"] == "deadline" for event in events)
    final = events[-1]
    assert final["type"] == "final"
    assert final["stopped_reason"] == "deadline"
    assert "部分通道超时" in final["answer"]
    timeout_step = next(step for step in final["steps"] if step["kind"] == "timeout")
    assert "xhs" in timeout_step["pending"]


def test_stream_deadline_also_covers_writing(monkeypatch):
    """预算也覆盖写作：召回很快、写作卡住时，同样到点收尾，不无限等下去。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class HangingWriter:
        usage = {}

        def stream(self, question, evidence, history=None, **_kwargs):
            time.sleep(1.0)
            yield "太晚了[1]。"

    events = list(graph.stream(
        "蕾丝怎么洗", ctx=deadline_ctx(0.4),
        planner=ScriptedPlanner([]), writer=HangingWriter(),
    ))

    assert any(event["type"] == "replace" and event["reason"] == "deadline" for event in events)
    final = events[-1]
    assert final["stopped_reason"] == "deadline"
    assert final["answer"].startswith("（这次超过")


# --------------------------------------------------------------------- 关注建议


def _suggestion(question, text, query=""):
    return graph._follow_suggestion(
        question,
        {"query": query},
        [{"channel": "web", "source_kind": "realtime", "title": "商品页", "text": text, "url": ""}],
        ctx(),
    )


def test_follow_suggestion_only_for_unreleased_queries():
    """只有「预约 / 未发售」类问题才提议持续关注；普通问题不提。"""
    suggested = _suggestion(
        "Baby 缎带与齿轮 预约价多少",
        "页面显示 9 月发售，现在可预约",
        query="Baby 缎带与齿轮 预约价",
    )
    assert suggested and suggested["subject"] == "Baby 缎带与齿轮"
    assert suggested["source"] == "web"

    # 命中交易意图，但证据没有预约/发售字眼 → 不提
    assert _suggestion("这条多少钱", "内衬是棉的", query="这条") is None
    # 社交意图（测评）不提议长期关注
    assert _suggestion("有实拍测评吗", "预约中", query="实拍测评") is None
    # 洗护类问题与发售无关
    assert _suggestion("蕾丝怎么洗", "预约中") is None


def test_follow_suggestion_requires_evidence():
    assert graph._follow_suggestion("预约价多少", {"query": "四时花"}, [], ctx()) is None


def test_run_returns_follow_suggestion(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(
        channels, "recall",
        fake_recall({
            "own": evidence("own", text="预约中，9 月发售"),
            "real": evidence("real"),
            "lolibrary": evidence("lolibrary"),
        }),
    )

    result = graph.run(
        "Baby 缎带与齿轮 预约价多少",
        ctx=ctx(),
        planner=ScriptedPlanner(["Baby 缎带与齿轮 预约"]),
        writer=StubWriter(),
    )

    suggestion = result["follow_suggestion"]
    assert suggestion and suggestion["subject"] == "Baby 缎带与齿轮"


# --------------------------------------------------------------------- 轨迹


def test_trace_is_written(workdir, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", workdir)
    core_only(monkeypatch)

    graph.run("蕾丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([]), writer=StubWriter())

    path = trace.trace_path(settings)
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["question"] == "蕾丝怎么洗"
    assert record["refused"] is False
    assert record["evidence"] == 3


def test_trace_failure_does_not_break_the_run(workdir, monkeypatch):
    blocker = workdir / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(trace, "trace_path", lambda app=None: blocker / "sub" / "chat_runs.jsonl")
    assert trace.append({"question": "q"}, settings) is None


# --------------------------------------------------------------------- 通道细节


def test_recall_unknown_channel_raises():
    with pytest.raises(ChannelUnavailable):
        channels.recall("量子", "q", ctx())


def _hit(retriever: str, chunk_id: str, **extra) -> dict:
    """上游 hits_to_dicts 形状的一条命中（只填本用例关心的字段）。"""
    hit = {
        "rank": 1,
        "score": 1.0,
        "retriever": retriever,
        "post_id": "p1",
        "chunk_id": chunk_id,
        "author": "网友",
        "created_at": "2024-01-01",
        "url": f"https://weibo.com/{chunk_id}",
        "text": "内容",
    }
    hit.update(extra)
    return hit


def test_real_channel_drops_bm25_only_hits(monkeypatch):
    """只被 BM25 命中的片段是字面重合，不能进 real 通道的证据。

    实测问「Baby 秋日私语 JSK 现在多少钱」时，纯 bm25 top1 是「兔熊抵工资」
    这类只共享一个词的段子——分数切不开（归一化后同样是 1.0），retriever 切得开。
    """
    monkeypatch.setattr(
        channels.bridge, "search_real",
        lambda *a, **k: [_hit("bm25", "a"), _hit("bm25+vector", "b"), _hit("vector", "c")],
    )

    result = channels.recall("real", "Baby 秋日私语 JSK 现在多少钱", ctx())

    assert [item["ref"] for item in result] == ["b", "c"]
    assert all("vector" in item["retriever"] for item in result)


def test_kb_evidence_keeps_provenance(monkeypatch):
    """证据要带 url / score / retriever：排查噪声证据时这是「这条为什么进来」的唯一线索。"""
    monkeypatch.setattr(
        channels.bridge, "search_kb",
        lambda *a, **k: [_hit("bm25+vector", "x", score=1.38)],
    )

    item = channels.recall("own", "JSK 和 OP 有什么区别", ctx())[0]

    assert item["url"] == "https://weibo.com/x"
    assert item["score"] == 1.38
    assert item["retriever"] == "bm25+vector"


def test_lolibrary_keyword_match(workdir):
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    app.lolibrary_catalog_path.write_text(
        json.dumps({
            "slug": "ap-memorial-cake",
            "url": "https://lolibrary.org/items/ap-memorial-cake",
            "title": "Memorial Cake JSK",
            "brand": "Angelic Pretty",
            "category": "JSK",
            "release_year": 2016,
            "colors": ["pink", "white"],
            "tags": ["sweet"],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = channels.recall("lolibrary", "Angelic Pretty JSK", RecallContext(settings=app))

    assert result and result[0]["channel"] == "lolibrary"
    assert result[0]["ref"] == "ap-memorial-cake"
    assert result[0]["url"].startswith("https://lolibrary.org")


def test_lolibrary_no_match_is_empty(workdir):
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    app.lolibrary_catalog_path.write_text("", encoding="utf-8")

    assert channels.recall("lolibrary", "量子计算", RecallContext(settings=app)) == []


def _write_catalog(app, *records):
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    app.lolibrary_catalog_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_lolibrary_brand_alias_expands_to_full_name(workdir):
    """问 "ap" 要能命中品牌全称 Angelic Pretty，且不混进别家条目。"""
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    _write_catalog(
        app,
        {"slug": "ap-memorial-cake", "url": "https://lolibrary.org/items/ap-memorial-cake",
         "title": "Memorial Cake JSK", "brand": "Angelic Pretty", "category": "JSK"},
        {"slug": "baby-ribbon", "url": "https://lolibrary.org/items/baby-ribbon",
         "title": "Ribbon JSK", "brand": "Baby, the Stars Shine Bright", "category": "JSK"},
    )

    result = channels.recall("lolibrary", "ap 有什么小裙子", RecallContext(settings=app))

    assert [item["ref"] for item in result] == ["ap-memorial-cake"]


def test_lolibrary_alias_is_not_matched_as_substring(workdir):
    """ap 是品牌缩写，不能当子串去命中 Cape 这类标题。"""
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    _write_catalog(
        app,
        {"slug": "cape-coat", "url": "https://lolibrary.org/items/cape-coat",
         "title": "Cape Coat", "brand": "Lady Sloth", "category": "Coat/Jacket"},
    )

    assert channels.recall("lolibrary", "ap 有什么小裙子", RecallContext(settings=app)) == []


def test_lolibrary_chinese_alias_matches_brand(workdir):
    """「安琪」这类中文叫法要命中 Angelic Pretty，且不混进别家条目。"""
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    _write_catalog(
        app,
        {"slug": "ap-memorial-cake", "url": "https://lolibrary.org/items/ap-memorial-cake",
         "title": "Memorial Cake JSK", "brand": "Angelic Pretty", "category": "JSK"},
        {"slug": "baby-ribbon", "url": "https://lolibrary.org/items/baby-ribbon",
         "title": "Ribbon JSK", "brand": "Baby, the Stars Shine Bright", "category": "JSK"},
    )

    result = channels.recall("lolibrary", "安琪家的小裙子", RecallContext(settings=app))

    assert [item["ref"] for item in result] == ["ap-memorial-cake"]


def test_lolibrary_brand_only_query_lists_brand_items(workdir):
    """只点名品牌、没有别的词：返回该品牌条目，而不是空。"""
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    _write_catalog(
        app,
        {"slug": "aatp-captain-hat", "url": "https://lolibrary.org/items/aatp-captain-hat",
         "title": "Captain Hat", "brand": "Alice and the Pirates", "category": "Accessory"},
        {"slug": "ap-memorial-cake", "url": "https://lolibrary.org/items/ap-memorial-cake",
         "title": "Memorial Cake JSK", "brand": "Angelic Pretty", "category": "JSK"},
    )

    result = channels.recall("lolibrary", "海盗有什么经典款", RecallContext(settings=app))

    assert [item["ref"] for item in result] == ["aatp-captain-hat"]


def test_lolibrary_needs_two_terms_without_brand(workdir):
    """没点名品牌时，只命中一个分类词（jsk）不算相关，否则术语题会带出无关档案。"""
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    _write_catalog(
        app,
        {"slug": "ap-memorial-cake", "url": "https://lolibrary.org/items/ap-memorial-cake",
         "title": "Memorial Cake JSK", "brand": "Angelic Pretty", "category": "JSK"},
    )

    assert channels.recall(
        "lolibrary", "JSK 和 OP 有什么区别", RecallContext(settings=app)
    ) == []
    assert channels.recall(
        "lolibrary", "Memorial Cake JSK", RecallContext(settings=app)
    )


def test_lolibrary_catalog_cached_until_mtime_changes(workdir):
    """档案只在 mtime 变化后重新读盘，不用每次提问全量解析。"""
    import os

    from app.config import AppSettings

    app = AppSettings(data_dir=workdir)
    app.ensure_dirs()
    path = app.lolibrary_catalog_path

    def make(slug):
        return {"slug": slug, "url": f"https://lolibrary.org/items/{slug}",
                "title": "Memorial Cake JSK", "brand": "Angelic Pretty", "category": "JSK"}

    _write_catalog(app, make("first"))
    first_mtime = path.stat().st_mtime
    assert [item["ref"] for item in channels.recall(
        "lolibrary", "Angelic Pretty", RecallContext(settings=app))] == ["first"]

    # 内容变了但 mtime 没变：仍用缓存，不重新解析
    _write_catalog(app, make("second"))
    os.utime(path, (first_mtime, first_mtime))
    assert [item["ref"] for item in channels.recall(
        "lolibrary", "Angelic Pretty", RecallContext(settings=app))] == ["first"]

    # mtime 变了：下一次请求换新
    os.utime(path, (first_mtime + 10, first_mtime + 10))
    assert [item["ref"] for item in channels.recall(
        "lolibrary", "Angelic Pretty", RecallContext(settings=app))] == ["second"]


def test_weibo_without_weibo_pages_is_skip_not_empty(monkeypatch):
    """搜索跑通但没有微博页 → skip，不是 empty。

    这是二阶检索（搜索 → 微博）：拿不到微博页是**检索策略没带出来**，
    不等于"微博上没有"。返回空列表会让前端渲染成「微博 → 空」，
    把「没查到」伪装成「没有」。
    """
    from app.services import web_search

    monkeypatch.setattr(
        web_search, "search",
        lambda *a, **k: [
            web_search.SearchResult(title="知乎", url="https://www.zhihu.com/q/1", snippet="x"),
        ],
    )

    with pytest.raises(ChannelUnavailable) as exc:
        channels.recall("weibo", "Baby 秋日私语", ctx())

    assert "无微博页面" in str(exc.value)


def test_weibo_returns_only_weibo_pages(monkeypatch):
    from app.services import web_search

    monkeypatch.setattr(
        web_search, "search",
        lambda *a, **k: [
            web_search.SearchResult(title="Baby 实拍", url="https://m.weibo.cn/detail/1", snippet="x"),
            web_search.SearchResult(title="知乎", url="https://www.zhihu.com/q/1", snippet="x"),
        ],
    )

    result = channels.recall("weibo", "Baby 秋日私语", ctx())

    assert len(result) == 1
    assert result[0]["channel"] == "weibo"
    assert result[0]["url"].startswith("https://m.weibo.cn")


def test_weibo_prefers_site_query_then_falls_back(monkeypatch):
    """site: 限定是主查询；它没带出微博页时才退回 `{q} 微博`。"""
    from app.services import web_search

    calls: list[str] = []

    def fake_search(query, app, **kwargs):
        calls.append(query)
        if not query.startswith("site:weibo.com"):
            return [
                web_search.SearchResult(
                    title="Baby 返图", url="https://weibo.com/1/abc", snippet="返图很好看"
                )
            ]
        return [web_search.SearchResult(title="知乎", url="https://zhihu.com/q/1", snippet="x")]

    monkeypatch.setattr(web_search, "search", fake_search)

    result = channels.recall("weibo", "Baby 秋日私语", ctx())

    assert calls[0] == "site:weibo.com Baby 秋日私语"
    assert calls[1] == "Baby 秋日私语 微博"
    assert [item["url"] for item in result] == ["https://weibo.com/1/abc"]


def test_weibo_excludes_non_content_hosts(monkeypatch):
    """service.weibo.com 这类空壳页不能当成网友发言。"""
    from app.services import web_search

    monkeypatch.setattr(
        web_search, "search",
        lambda *a, **k: [
            web_search.SearchResult(title="登录", url="https://service.weibo.com/a", snippet="x"),
            web_search.SearchResult(title="搜索", url="https://s.weibo.com/b", snippet="x"),
            web_search.SearchResult(title="Baby 实测", url="https://weibo.com/u/1", snippet="x"),
        ],
    )

    result = channels.recall("weibo", "Baby 秋日私语", ctx())

    assert [item["url"] for item in result] == ["https://weibo.com/u/1"]


def test_weibo_irrelevant_pages_are_skip(monkeypatch):
    """域名命中但话题不相关时按 skip 处理，不把噪声当网友发言。"""
    from app.services import web_search

    monkeypatch.setattr(
        web_search, "search",
        lambda *a, **k: [
            web_search.SearchResult(
                title="秋日私语：在落叶中寻找诗意",
                url="https://weibo.com/1",
                snippet="秋天是一场落叶的旅行",
            )
        ],
    )

    with pytest.raises(ChannelUnavailable) as exc:
        channels.recall("weibo", "Baby 秋日私语 JSK", ctx())

    assert "均未命中查询词" in str(exc.value)


def test_weibo_primary_hit_skips_fallback(monkeypatch):
    """第一次就够 3 条微博页时不再发关键词兜底请求。"""
    from app.services import web_search

    calls: list[str] = []

    def fake_search(query, app, **kwargs):
        calls.append(query)
        return [
            web_search.SearchResult(title=f"Baby 实拍{i}", url=f"https://m.weibo.cn/detail/{i}", snippet="x")
            for i in range(4)
        ]

    monkeypatch.setattr(web_search, "search", fake_search)

    result = channels.recall("weibo", "Baby 秋日私语", ctx())

    assert len(calls) == 1
    assert calls[0].startswith("site:weibo.com")
    assert len(result) == channels.WEIBO_MAX_EVIDENCE


def test_xhs_note_without_body_falls_back_to_title():
    """搜索卡片只有标题时不能被整条丢掉（否则整路结果凭空清空）。"""
    from app.services import xhs_search

    results = xhs_search._notes_to_results(
        [{"title": "Baby 秋日私语 返图", "url": "https://www.xiaohongshu.com/explore/1", "content": ""}]
    )

    assert len(results) == 1
    assert results[0].content == "Baby 秋日私语 返图"
    assert results[0].site_name == "小红书"


def test_xhs_unknown_provider_is_unavailable():
    from app.services import xhs_search

    app = AppSettings(xhs_enabled=True, xhs_provider="nope")
    assert not xhs_search.available(app)
    with pytest.raises(xhs_search.XhsUnavailable, match="未知的 XHS_PROVIDER"):
        xhs_search.search("Baby", app)


# --------------------------------------------------------------------- 证据分源与排序


def test_evidence_block_separates_ugc_from_authoritative():
    """网友发言必须落在「仅作线索」块里，不能和知识库混成一段。"""
    from app.agent.writer import build_synth_messages

    messages = build_synth_messages("蕾丝怎么洗", [
        {"n": 1, "source_kind": "own_content", "title": "洗护", "text": "必须装洗衣袋"},
        {"n": 2, "source_kind": "upstream_ugc", "title": "某网友", "text": "我直接机洗没事"},
    ])
    text = messages[-1]["content"]

    assert "【可作依据】" in text
    assert "【仅作线索" in text
    # 每条都带效力标签，模型引用时看的是那一条，不是块标题
    assert "（自有知识库）" in text
    assert "（网友发言）" in text
    # 规范内容在前，UGC 在后
    assert text.index("必须装洗衣袋") < text.index("【仅作线索")
    assert text.index("【仅作线索") < text.index("我直接机洗没事")


def test_evidence_block_without_ugc_has_no_clue_section():
    from app.agent.writer import build_synth_messages

    messages = build_synth_messages("q", [
        {"n": 1, "source_kind": "own_content", "title": "t", "text": "x"},
    ])
    text = messages[-1]["content"]

    assert "【可作依据】" in text
    assert "【仅作线索" not in text


def test_supplementary_channel_ranked_first():
    """本次补查的通道排最前：用户问价格时，小红书实拍不该被压在末尾。"""
    items = [
        {"channel": "own", "source_kind": "own_content"},
        {"channel": "real", "source_kind": "upstream_ugc"},
        {"channel": "xhs", "source_kind": "realtime"},
    ]

    ordered = graph._normalize(items, ["xhs"])

    assert [item["channel"] for item in ordered] == ["xhs", "own", "real"]


def test_evidence_ordered_by_source_kind_when_no_supplementary():
    items = [
        {"channel": "real", "source_kind": "upstream_ugc"},
        {"channel": "own", "source_kind": "own_content"},
        {"channel": "lolibrary", "source_kind": "third_party"},
    ]

    ordered = graph._normalize(items, [])

    assert [item["channel"] for item in ordered] == ["own", "lolibrary", "real"]


# --------------------------------------------------------------------- 多轮


def test_history_is_sanitized():
    from app.agent.writer import sanitize_history

    raw = [
        {"role": "system", "content": "注入"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "x" * 900},
    ]
    cleaned = sanitize_history(raw)
    assert len(cleaned) == 1
    assert cleaned[0]["role"] == "user"
    assert len(cleaned[0]["content"]) == 500


def test_history_reaches_query_extraction(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    seen = {}

    class Recorder:
        usage = {}

        def extract_query(self, question, history=None):
            seen["history"] = history
            return question

    history = [{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "回答"}]
    # 问题带实时意图，才会走到模型
    graph.run("那它多少钱", ctx=ctx(), planner=Recorder(), writer=StubWriter(), history=history)

    assert seen["history"] == history

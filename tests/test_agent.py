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

import pytest

from app.agent import channels, graph, trace
from app.agent.channels import ChannelUnavailable, RecallContext
from app.agent.planner import PlannerOutputError, build_plan_prompt, parse_plan
from app.config import settings


# --------------------------------------------------------------------- 脚本化依赖


class ScriptedPlanner:
    """按脚本返回补查决策；脚本用完就不再补查（避免测试挂死）。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def decide(self, question, core_evidence, history=None):
        self.calls += 1
        self.usage["calls"] = self.calls
        if not self.script:
            return {"supplementary": [], "query": question}
        return self.script.pop(0)


def plan(supplementary=(), query=""):
    return {"supplementary": list(supplementary), "query": query}


class StubWriter:
    usage = {"calls": 1}

    def write(self, question, evidence, history=None):
        return "[1] 装洗衣袋。"

    def stream(self, question, evidence, history=None):
        yield "蕾丝"
        yield "要装洗衣袋[1]。"


def ctx() -> RecallContext:
    return RecallContext(settings=settings)


def evidence(channel, ref=None, title=None, text="内容", url=""):
    return [{
        "channel": channel,
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


@pytest.mark.parametrize("text, expected", [
    ('{"supplementary":["xhs","web"],"query":"Baby 缎带"}', ["xhs", "web"]),
    ('{"supplementary":[],"query":""}', []),
    ('{"supplementary":["xhs"]}', ["xhs"]),       # 不传 query 也接受
])
def test_parse_plan_accepts_valid(text, expected):
    assert parse_plan(text)["supplementary"] == expected


@pytest.mark.parametrize("text, fragment", [
    ("不是 JSON", "合法 JSON"),
    ('{"supplementary":"xhs"}', "字符串数组"),
    ('["数组"]', "JSON 对象"),
])
def test_parse_plan_rejects_invalid(text, fragment):
    with pytest.raises(PlannerOutputError) as exc:
        parse_plan(text)
    assert fragment in str(exc.value)


def test_plan_prompt_reports_empty_core_and_realtime_rule():
    prompt = build_plan_prompt("Baby 有什么新的", [])
    assert "核心通道没有召回任何内容" in prompt
    assert "空结果是有效结果" in prompt
    assert "supplementary" in prompt


# --------------------------------------------------------------------- 编排护栏


def test_core_channels_recalled_in_parallel(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    planner = ScriptedPlanner([plan()])

    result = graph.run("四时花怎么洗", ctx=ctx(), planner=planner, writer=StubWriter())

    channels_seen = {item["channel"] for item in result["evidence"]}
    assert channels_seen == {"own", "real", "lolibrary"}
    assert [item["n"] for item in result["evidence"]] == [1, 2, 3]
    assert result["refused"] is False
    assert planner.calls == 1


def test_supplementary_added_when_planner_requests(monkeypatch):
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
    planner = ScriptedPlanner([plan(["xhs"], "四时花 预约价")])

    result = graph.run("四时花 预约价是多少", ctx=ctx(), planner=planner, writer=StubWriter())

    seen = {item["channel"] for item in result["evidence"]}
    assert "xhs" in seen
    assert any(step["kind"] == "route" and step["supplementary"] == ["xhs"] for step in result["steps"])


def test_no_evidence_is_refused(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({}))
    planner = ScriptedPlanner([plan()])

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
    planner = ScriptedPlanner([plan(["xhs"])])

    result = graph.run("四时花 预约价", ctx=ctx(), planner=planner, writer=StubWriter())

    assert "xhs" not in {item["channel"] for item in result["evidence"]}
    assert any(step["kind"] == "skip" and step["channel"] == "xhs" for step in result["steps"])


def test_route_planner_failure_degrades_to_core_only(monkeypatch):
    """补查决策是可选优化：planner 挂了就退回仅核心通道，不影响作答。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BrokenPlanner:
        usage = {}

        def decide(self, question, core_evidence, history=None):
            raise PlannerOutputError("模型没有返回合法 JSON")

    result = graph.run("q", ctx=ctx(), planner=BrokenPlanner(), writer=StubWriter())

    assert result["refused"] is False
    assert {item["channel"] for item in result["evidence"]} == {"own", "real", "lolibrary"}
    assert any(step["kind"] == "route" and step["error"] for step in result["steps"])


def test_out_of_range_citation_is_downgraded(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BadWriter:
        usage = {}

        def write(self, question, evidence, history=None):
            return "有 [9] 条。"

    result = graph.run("q", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=BadWriter())

    assert "引用编号对不上" in result["answer"]
    assert result["stopped_reason"] == "citation_out_of_range"


def test_refusal_with_evidence_is_kept(monkeypatch):
    """模型说"没找到"时本来就不该有引用，不该被硬判成错误。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class RefusingWriter:
        usage = {}

        def write(self, question, evidence, history=None):
            return "没有找到关于真丝的规定。"

    result = graph.run("真丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=RefusingWriter())

    assert result["answer"] == "没有找到关于真丝的规定。"
    assert result["stopped_reason"] != "no_citation"


# --------------------------------------------------------------------- 流式


def test_stream_emits_steps_then_delta_then_final(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    events = list(graph.stream("蕾丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=StubWriter()))

    kinds = [event["type"] for event in events]
    assert kinds[0] == "step"
    assert "delta" in kinds
    assert kinds[-1] == "final"
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    assert text == "蕾丝要装洗衣袋[1]。"
    assert events[-1]["answer"] == "蕾丝要装洗衣袋[1]。"


def test_stream_replaces_bad_citation(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class BadWriter:
        usage = {}

        def stream(self, question, evidence, history=None):
            yield "有 [7] 条。"

    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=BadWriter()))

    assert any(event["type"] == "replace" for event in events)
    assert "引用编号对不上" in events[-1]["answer"]


def test_stream_refuses_without_evidence(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    monkeypatch.setattr(channels, "recall", fake_recall({}))
    events = list(graph.stream("有吗", ctx=ctx(), planner=ScriptedPlanner([plan()])))

    assert events[-1]["refused"] is True
    assert "不猜" in events[-1]["answer"]


def test_stream_writer_failure_falls_back(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)

    class DeadWriter:
        usage = {}

        def stream(self, *_args, **_kwargs):
            raise RuntimeError("上游 502")

    events = list(graph.stream("q", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=DeadWriter()))

    assert any(event["type"] == "replace" and event["reason"] == "writer_failed" for event in events)
    assert events[-1]["answer"]


# --------------------------------------------------------------------- 轨迹


def test_trace_is_written(workdir, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", workdir)
    core_only(monkeypatch)

    graph.run("蕾丝怎么洗", ctx=ctx(), planner=ScriptedPlanner([plan()]), writer=StubWriter())

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


def test_history_reaches_planner(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    core_only(monkeypatch)
    seen = {}

    class Recorder:
        usage = {}

        def decide(self, question, core_evidence, history=None):
            seen["core"] = core_evidence
            seen["history"] = history
            return plan()

    history = [{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "回答"}]
    graph.run("这一轮", ctx=ctx(), planner=Recorder(), writer=StubWriter(), history=history)

    assert seen["history"] == history
    assert len(seen["core"]) == 3

"""模型分级路由：规则优先、可解释、失败能兜底。"""

from __future__ import annotations

import json

import pytest

from app.agent import channels, graph, routing
from app.agent.channels import RecallContext
from app.config import settings

# 规则判不出来：无补查、无综合词、长度超过阈值。
AMBIGUOUS = "我印象里好像见过一条很特别的裙子但是完全想不起来它叫什么名字了"


class StubPlanner:
    def __init__(self):
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def extract_query(self, question, history=None):
        return question


class CapturingWriter:
    def __init__(self):
        self.models: list = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def write(self, question, evidence, history=None, **kwargs):
        self.models.append(kwargs.get("model"))
        return "[1] 装洗衣袋。"

    def stream(self, question, evidence, history=None, **kwargs):
        self.models.append(kwargs.get("model"))
        yield "[1] 装洗衣袋。"


class FakeJudge:
    def __init__(self, answer="strong", usage=None):
        self.answer = answer
        self.usage = usage or {
            "calls": 1,
            "prompt_tokens": 12,
            "completion_tokens": 1,
            "calls_detail": [
                {
                    "role": "router",
                    "model": "cheap-x",
                    "prompt_tokens": 12,
                    "completion_tokens": 1,
                }
            ],
        }

    def judge(self, question):
        return self.answer


class BoomJudge:
    def judge(self, question):
        raise TimeoutError("超时")


def configure(monkeypatch, **overrides):
    values = {
        "model_routing_mode": "auto",
        "model_routing_llm": True,
        "llm_model": "base-model",
        "llm_model_cheap": "cheap-x",
        "llm_model_strong": "strong-x",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def one_evidence(name, question, ctx, *, query=""):
    return [
        {
            "channel": "own",
            "source_kind": "own_content",
            "ref": "own",
            "title": "蕾丝洗护",
            "text": "装洗衣袋。",
            "url": "",
        }
    ]


# --------------------------------------------------------------------- 规则


@pytest.mark.parametrize(
    "question, expected_rule",
    [
        ("这条裙子多少钱", "multi_channel"),          # 价格 → xhs + web
        ("有实拍测评吗", "multi_channel"),            # 社交 → xhs + weibo
        ("最近有什么新款", "single_channel"),          # 上新 → 只走 web
        ("蕾丝怎么洗", "simple_qa"),
    ],
)
def test_rules_pick_tier_with_reason(question, expected_rule):
    tier, features, rule, reason = routing.classify_question(question)
    assert rule == expected_rule
    assert reason
    assert any(item.startswith("channels=") for item in features)
    if expected_rule == "multi_channel":
        assert tier == routing.TIER_STRONG
    elif expected_rule in ("single_channel", "simple_qa"):
        assert tier == routing.TIER_CHEAP


def test_synthesis_words_alone_push_to_strong():
    """没有补查、也不长，但「哪个好」需要跨证据推理，仍走强档。"""
    tier, features, rule, _ = routing.classify_question("这两条裙子哪个好")
    assert tier == routing.TIER_STRONG
    assert rule == "synthesis"
    assert any(item.startswith("synthesis=") for item in features)


def test_ambiguous_returns_none():
    tier, _, rule, _ = routing.classify_question(AMBIGUOUS)
    assert tier is None
    assert rule == "ambiguous"


# --------------------------------------------------------------------- 模式


@pytest.mark.parametrize("mode, model", [("cheap", "cheap-x"), ("strong", "strong-x")])
def test_forced_mode_skips_rules_and_judge(monkeypatch, mode, model):
    configure(monkeypatch, model_routing_mode=mode)
    decision = routing.decide(AMBIGUOUS, settings, judge=BoomJudge())
    assert decision.mode == mode
    assert decision.tier == mode
    assert decision.model == model
    assert decision.rule == "forced"
    assert f"mode={mode}" in decision.features


def test_unknown_mode_falls_back_to_auto(monkeypatch):
    configure(monkeypatch, model_routing_mode="whatever")
    decision = routing.decide("蕾丝怎么洗", settings)
    assert decision.mode == "auto"
    assert decision.rule == "simple_qa"


# --------------------------------------------------------------------- 轻量判断


def test_judge_used_only_when_rules_cannot_decide(monkeypatch):
    configure(monkeypatch)
    decision = routing.decide(AMBIGUOUS, settings, judge=FakeJudge("strong"))
    assert decision.tier == "strong"
    assert decision.rule == "llm"
    assert len(decision.calls) == 1
    assert decision.calls[0]["role"] == "router"


def test_judge_failure_falls_back_to_cheap_without_raising(monkeypatch):
    """判断失败/超时是**兜底**，不是错误：整次回答照常进行。"""
    configure(monkeypatch)
    decision = routing.decide(AMBIGUOUS, settings, judge=BoomJudge())
    assert decision.tier == "cheap"
    assert decision.rule == "llm_failed"
    assert decision.model == "cheap-x"


def test_judge_disabled_falls_back_to_cheap(monkeypatch):
    configure(monkeypatch, model_routing_llm=False)
    decision = routing.decide(AMBIGUOUS, settings, judge=FakeJudge("strong"))
    assert decision.tier == "cheap"
    assert decision.rule == "ambiguous_default"


def test_unclear_judge_output_is_safe_default(monkeypatch):
    configure(monkeypatch)
    decision = routing.decide(AMBIGUOUS, settings, judge=FakeJudge("不知道"))
    assert decision.tier == "cheap"
    assert decision.rule == "llm"


def test_model_name_falls_back_to_base(monkeypatch):
    configure(monkeypatch, llm_model_cheap="", llm_model_strong="")
    decision = routing.decide("蕾丝怎么洗", settings)
    assert decision.model == "base-model"


# --------------------------------------------------------------------- 接线


def test_run_uses_routed_model_and_records_mode(monkeypatch, data_dir):
    """路由结果必须真的落到 writer，且账本里带 mode（A/B 的前提）。"""
    configure(monkeypatch, model_routing_mode="strong")
    monkeypatch.setattr(channels, "recall", one_evidence)
    writer = CapturingWriter()

    payload = graph.run(
        "蕾丝怎么洗",
        ctx=RecallContext(settings=settings),
        planner=StubPlanner(),
        writer=writer,
    )

    assert writer.models == ["strong-x"]
    assert payload["route"]["rule"] == "forced"
    assert payload["route"]["tier"] == "strong"
    assert payload["cost"]["mode"] == "strong"
    assert payload["cost"]["tier"] == "strong"

    records = tracing_records(data_dir)
    assert records[-1]["route"]["tier"] == "strong"
    assert records[-1]["cost"]["mode"] == "strong"


def test_stream_uses_routed_model(monkeypatch, data_dir):
    configure(monkeypatch, model_routing_mode="cheap")
    monkeypatch.setattr(channels, "recall", one_evidence)
    writer = CapturingWriter()

    events = list(
        graph.stream(
            "蕾丝怎么洗",
            ctx=RecallContext(settings=settings),
            planner=StubPlanner(),
            writer=writer,
        )
    )

    final = [item for item in events if item.get("type") == "final"][-1]
    assert writer.models == ["cheap-x"]
    assert final["route"]["mode"] == "cheap"
    assert final["cost"]["mode"] == "cheap"


def tracing_records(data_dir):
    path = data_dir / "logs" / "chat_runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
